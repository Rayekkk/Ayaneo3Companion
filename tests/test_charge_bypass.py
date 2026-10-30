# SPDX-License-Identifier: BSD-3-Clause
"""Independent charge transactions must remain responsive during TDP recovery."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import types
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
import main
import tdp_backend
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager


class IndependentChargeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.store.replace({"charge_bypass": False, "controller": controller_backend.DEFAULT_CONTROLLER})
        self.state = {"supported": True, "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "charge_bypass": False,
                      "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(tdp_backend.Plugin, "_ready", False))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.hardware_charge = False
        self.charge_writes = []
        self.stack.enter_context(patch.object(ec_backend, "read_charge_bypass", side_effect=lambda: self.hardware_charge))
        self.stack.enter_context(patch.object(ec_backend, "write_charge_bypass", side_effect=self.write_charge))
        self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.plugin = main.Plugin()

    def write_charge(self, enabled):
        self.hardware_charge = enabled
        self.charge_writes.append(enabled)

    async def held_tdp(self, entered, release):
        def hold():
            with tdp_backend._mutation_lock:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test failed to release TDP transaction")
        return await asyncio.to_thread(hold)

    async def test_charge_ack_and_next_rgb_write_do_not_wait_for_tdp_monitor(self):
        entered, release = threading.Event(), threading.Event()
        tasks = [asyncio.create_task(self.held_tdp(entered, release))]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            tasks.append(asyncio.create_task(self.plugin.set_charge_bypass(True)))
            tasks.append(asyncio.create_task(self.plugin.set_controller(
                {**controller_backend.DEFAULT_CONTROLLER, "brightness": 40})))
            done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
            acknowledged = len(done)
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(acknowledged, 2, "charge and the next RGB write must complete during TDP recovery")
        self.assertTrue(all(response["ok"] for response in responses[1:]), responses)
        self.assertTrue(responses[1]["state"]["charge_bypass"])
        self.assertEqual(responses[2]["state"]["controller"]["brightness"], 40)
        self.assertTrue(self.store.getSetting("charge_bypass"))

    async def test_charge_drift_recovery_does_not_wait_for_tdp_transaction(self):
        self.store.replace({"charge_bypass": True, "controller": controller_backend.DEFAULT_CONTROLLER})
        entered, release = threading.Event(), threading.Event()
        tasks = [asyncio.create_task(self.held_tdp(entered, release))]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            tasks.append(asyncio.create_task(self.plugin._offload(
                self.plugin._restore_saved_control, "charge_bypass", True)))
            done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
            restored = bool(done)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertTrue(restored, "charge recovery is independent of TDP")
        self.assertTrue(self.hardware_charge)
        self.assertEqual(self.charge_writes, [True])

    async def test_game_heartbeat_does_not_wait_behind_a_controller_write(self):
        entered, release = threading.Event(), threading.Event()
        def held_apply(*_args, **_kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test failed to release the controller write")
        tasks = []
        with patch.object(controller_backend, "apply_controller", side_effect=held_apply):
            try:
                tasks.append(asyncio.create_task(self.plugin.set_controller(
                    {**controller_backend.DEFAULT_CONTROLLER, "brightness": 40})))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin.set_active_app("222")))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
                acknowledged = len(done)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(responses, [responses[0], {"ok": True}])
        self.assertIs(responses[0]["ok"], True)
        self.assertEqual(acknowledged, 1, "TDP's foreground-game lease never queues behind main's writes")
        self.assertEqual(tdp_backend._frontend_appid, "222")

    async def test_charge_monitor_waits_for_commit_and_uses_new_saved_intent(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_write(enabled):
            self.write_charge(enabled)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test failed to release charge write")
        tasks = []
        with patch.object(ec_backend, "write_charge_bypass", side_effect=delayed_write):
            try:
                tasks.append(asyncio.create_task(self.plugin.set_charge_bypass(True)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin._offload(
                    self.plugin._restore_saved_control, "charge_bypass", True)))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.1)
                raced = bool(done)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertIs(responses[0]["ok"], True)
        self.assertFalse(raced, "the charge monitor must wait for the public transaction's commit")
        self.assertEqual(self.charge_writes, [True])
        self.assertTrue(self.hardware_charge)
        self.assertTrue(self.store.getSetting("charge_bypass"))


class ChargeMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.store.replace({"controller": controller_backend.DEFAULT_CONTROLLER})
        self.state = {"supported": True, "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "charge_bypass": False, "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.stack.enter_context(patch.object(ec_backend, "both_modules_connected", return_value=False))
        self.stack.enter_context(patch.object(main, "_updates", return_value=types.SimpleNamespace(close=Mock())))
        for name in ("_restore_task", "_edid_task", "_charge_task", "_module_task",
                     "_tm_guard_task", "_vibration_gain_task", "_audio_task"):
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        self.plugin = main.Plugin()

    async def asyncTearDown(self):
        await asyncio.wait_for(self.plugin._unload(), 3)

    async def test_charge_monitor_checks_on_ac_resume_and_periodic_interval(self):
        clock = 0.0
        checks = []
        async def tick(delay):
            nonlocal clock
            clock += delay
            if clock > 30:
                raise asyncio.CancelledError
        def verify(key, only_if_changed):
            checks.append(clock)
            self.assertEqual((key, only_if_changed), ("charge_bypass", True))
        with patch.object(main, "time", types.SimpleNamespace(monotonic=lambda: clock)), \
             patch.object(main.asyncio, "sleep", side_effect=tick), \
             patch.object(ec_backend, "ac_online", side_effect=lambda: clock >= 3), \
             patch.object(main, "_suspend_clock_offset", side_effect=lambda: 2.0 if clock >= 6 else 0.0), \
             patch.object(self.plugin, "_restore_saved_control", side_effect=verify):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._charge_loop()
        self.assertEqual(checks, [1.0, 3.0, 6.0, 21.0])

    async def test_failed_charge_check_retries_without_busy_loop(self):
        clock = 0.0
        checks = []
        async def tick(delay):
            nonlocal clock
            clock += delay
            if clock > 20:
                raise asyncio.CancelledError
        def verify(key, only_if_changed):
            checks.append(clock)
            if len(checks) == 1:
                raise RuntimeError("charge interface temporarily absent")
        with patch.object(main, "time", types.SimpleNamespace(monotonic=lambda: clock)), \
             patch.object(main.asyncio, "sleep", side_effect=tick), \
             patch.object(ec_backend, "ac_online", return_value=True), \
             patch.object(main, "_suspend_clock_offset", return_value=0.0), \
             patch.object(self.plugin, "_restore_saved_control", side_effect=verify):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._charge_loop()
        self.assertEqual(checks, [1.0, 16.0])

    async def test_unload_cancels_charge_monitor_and_drains_its_active_transaction(self):
        entered, release = threading.Event(), threading.Event()
        sleep = asyncio.sleep
        async def tick(delay):
            await sleep(0.001)
        def read():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release charge read")
            return False
        self.store.replace({"charge_bypass": True, "controller": controller_backend.DEFAULT_CONTROLLER})
        with patch.object(main.asyncio, "sleep", side_effect=tick), \
             patch.object(ec_backend, "ac_online", return_value=True), \
             patch.object(main, "_suspend_clock_offset", return_value=0.0), \
             patch.object(ec_backend, "read_charge_bypass", side_effect=read), \
             patch.object(ec_backend, "write_charge_bypass") as write:
            monitor = asyncio.create_task(self.plugin._charge_loop())
            main.Plugin._charge_task = monitor
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                unloading = asyncio.create_task(self.plugin._unload())
                await sleep(0.01)
                self.assertFalse(unloading.done())
            finally:
                release.set()
                await asyncio.wait_for(unloading, 3)
        self.assertTrue(monitor.cancelled())
        self.assertIsNone(main.Plugin._charge_task)
        self.assertFalse(self.plugin._workers)
        write.assert_called_once_with(True)


class BatteryAndChargeTests(unittest.TestCase):
    def test_battery_status_uses_upower_time_to_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Charging", "capacity": "82",
                "energy_now": "37826000", "energy_full": "46130000",
                "power_now": "5139000", "voltage_now": "11550000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full", return_value=5869):
                value = ec_backend.battery_status()
        self.assertEqual(value["percent"], 82)
        self.assertEqual(value["status"], "Charging")
        self.assertEqual(value["seconds_to_full"], 5869)
        self.assertEqual(value["power_w"], 5.14)
        self.assertEqual(value["source"], "UPower")

    def test_battery_status_falls_back_to_energy_and_power(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Charging", "capacity": "82",
                "energy_now": "37826000", "energy_full": "46130000",
                "power_now": "5139000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full", return_value=None):
                value = ec_backend.battery_status()
        self.assertEqual(value["seconds_to_full"], 5817)
        self.assertEqual(value["source"], "sysfs")

    def test_battery_status_does_not_estimate_while_discharging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Discharging", "capacity": "70",
                "energy_now": "32000000", "energy_full": "46130000", "power_now": "8000000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full") as upower:
                value = ec_backend.battery_status()
        upower.assert_not_called()
        self.assertIsNone(value["seconds_to_full"])

    def test_charge_bypass_register_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io"
            path.write_bytes(bytes(256))
            old_path = ec_backend._ec_io_path
            old_behaviour = ec_backend._charge_behaviour_path
            old_ensure = ec_backend.ensure_charge_bypass_control
            old_supported = companion_common.supported_device
            try:
                ec_backend._ec_io_path = lambda: path
                ec_backend._charge_behaviour_path = lambda: None
                ec_backend.ensure_charge_bypass_control = lambda: path
                companion_common.supported_device = lambda: True
                self.assertFalse(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(True)
                self.assertTrue(ec_backend.read_charge_bypass())
                self.assertEqual(path.read_bytes()[ec_backend.EC_CHARGE_REGISTER], ec_backend.EC_CHARGE_INHIBIT)
                ec_backend.write_charge_bypass(False)
                self.assertFalse(ec_backend.read_charge_bypass())
                self.assertEqual(path.read_bytes()[ec_backend.EC_CHARGE_REGISTER], ec_backend.EC_CHARGE_AUTO)
            finally:
                ec_backend._ec_io_path = old_path
                ec_backend._charge_behaviour_path = old_behaviour
                ec_backend.ensure_charge_bypass_control = old_ensure
                companion_common.supported_device = old_supported

    def test_failed_charge_rollback_names_the_reason_without_the_sysfs_path(self):
        behaviour = mock.Mock()
        behaviour.write_text.side_effect = PermissionError(
            13, "Permission denied", "/sys/class/power_supply/BAT0/charge_behaviour")
        with mock.patch.object(companion_common, "supported_device", return_value=True),              mock.patch.object(ec_backend, "ensure_charge_bypass_control", return_value=behaviour),              mock.patch.object(ec_backend, "_charge_behaviour_path", return_value=behaviour),              mock.patch.object(ec_backend, "_read_charge_behaviour", return_value=False):
            with self.assertRaises(RuntimeError) as raised:
                ec_backend.write_charge_bypass(True)
        self.assertEqual(str(raised.exception), "Permission denied; charge recovery failed: Permission denied")

    def test_apply_and_save_names_the_reason_without_the_settings_path(self):
        def apply():
            raise PermissionError(13, "Permission denied", "/home/deck/homebrew/settings/x.json")
        with self.assertRaises(RuntimeError) as raised:
            main.Plugin._apply_and_save(apply, lambda: None, {})
        self.assertEqual(str(raised.exception), "Permission denied; previous hardware settings restored")

    def test_charge_bypass_prefers_kernel_charge_behaviour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            (battery / "type").write_text("Battery\n")
            behaviour = battery / "charge_behaviour"
            behaviour.write_text("[auto] inhibit-charge\n")
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(companion_common, "supported_device", return_value=True), \
                 mock.patch.object(ec_backend, "_ec_io_path") as ec_path:
                self.assertEqual(ec_backend.ensure_charge_bypass_control(), behaviour)
                self.assertFalse(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(True)
                self.assertTrue(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(False)
                self.assertFalse(ec_backend.read_charge_bypass())
            ec_path.assert_not_called()
