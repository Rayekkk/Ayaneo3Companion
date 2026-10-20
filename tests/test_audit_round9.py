# SPDX-License-Identifier: BSD-3-Clause
"""Independent fan controls and shutdown races; no live hardware writes."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_logic
import main
import power_backend
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager


class AuditRound9Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.store.replace({"controller": controller_backend.DEFAULT_CONTROLLER})
        self.state = {"supported": True, "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "tdp": dict(power_backend.DEFAULT_TDP), "tdp_supported": True,
                      "tdp_backend": "RyzenAdj", "tdp_error": ""}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "111"))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(power_backend, "read_cpu_boost", return_value=True))
        self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.stack.enter_context(patch.object(ec_backend, "both_modules_connected", return_value=False))
        self.stack.enter_context(patch.object(companion_common, "updater", types.SimpleNamespace(close=Mock())))
        self.stack.enter_context(patch.object(main.fan_controller, "close"))
        for name in ("_restore_task", "_edid_task", "_ac_task", "_charge_task", "_module_task",
                     "_tm_guard_task", "_vibration_gain_task", "_audio_task", "_fan_task"):
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        self.plugin = main.Plugin()

    async def asyncTearDown(self):
        await asyncio.wait_for(self.plugin._unload(), 3)

    @staticmethod
    def fan_result(mode, percent=None):
        return {"supported": True, "mode": mode, "percent": percent, "rpm": 3000}

    async def test_fan_auto_does_not_wait_for_slow_tdp_but_settings_still_serialize(self):
        entered, release = threading.Event(), threading.Event()
        def tdp(value):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release TDP write")
        tasks = []
        with patch.object(power_backend, "apply_tdp", side_effect=tdp), \
             patch.object(main.fan_controller, "set", side_effect=self.fan_result):
            try:
                tasks.append(asyncio.create_task(self.plugin.set_tdp(power_backend.PRESETS["Max"], "Max", "111")))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin.set_controller({"brightness": 40})))
                tasks.append(asyncio.create_task(self.plugin.set_fan("auto")))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
                fan_finished = tasks[2] in done
                controller_finished = tasks[1] in done
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertTrue(fan_finished, "Auto is independent of SMU/settings and must not wait behind TDP")
        self.assertFalse(controller_finished, "shared settings transactions must remain serialized")
        self.assertEqual(results[2]["mode"], "auto")
        self.assertEqual(self.store.getSetting("controller")["brightness"], 40)

    async def test_shutdown_during_manual_fan_write_cannot_start_late_monitor(self):
        entered, release = threading.Event(), threading.Event()
        def fan(mode, percent=None):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release fan write")
            return self.fan_result(mode, percent)
        with patch.object(main.fan_controller, "set", side_effect=fan), \
             patch.object(self.plugin, "_fan_loop", new=AsyncMock()) as monitor:
            writing = asyncio.create_task(self.plugin.set_fan("manual", 80))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                unloading = asyncio.create_task(self.plugin._unload())
                await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.wait_for(writing, 3)
                await asyncio.wait_for(unloading, 3)
            monitor.assert_not_called()
        self.assertIsNone(main.Plugin._fan_task)
        self.assertFalse(self.plugin._workers)
        main.fan_controller.close.assert_called_once()

    async def test_fan_commands_remain_ordered_while_auto_waits_for_manual(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def fan(mode, percent=None):
            calls.append(mode)
            if mode == "manual":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release manual write")
            return self.fan_result(mode, percent)
        with patch.object(main.fan_controller, "set", side_effect=fan), \
             patch.object(self.plugin, "_fan_loop", new=AsyncMock()):
            manual = asyncio.create_task(self.plugin.set_fan("manual", 80))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                automatic = asyncio.create_task(self.plugin.set_fan("auto"))
                await asyncio.sleep(0)
                self.assertEqual(calls, ["manual"])
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(manual, automatic), 3)
        self.assertEqual(calls, ["manual", "auto"])
        self.assertEqual(results[-1]["mode"], "auto")

    async def test_unavailable_plugin_blocks_fan_before_any_hardware_write(self):
        with patch.object(main.fan_controller, "set") as fan:
            self.state["supported"] = False
            with self.assertRaisesRegex(RuntimeError, "not an AYANEO"):
                await self.plugin.set_fan("auto")
            self.state.update(supported=True, startup_error="startup failed")
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                await self.plugin.set_fan("manual", 80)
            self.state.pop("startup_error")
            self.plugin._closing = True
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                await self.plugin.set_fan("auto")
        fan.assert_not_called()

    async def test_charge_drift_recovers_while_tdp_restore_is_stalled(self):
        entered, release, restored = threading.Event(), threading.Event(), threading.Event()
        hardware = {"charge_bypass": True}
        clock = 0.0
        sleep = asyncio.sleep
        async def tick(delay):
            nonlocal clock
            clock += delay
            await sleep(0.002)
        def apply(value):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release TDP restore")
        def charge(value):
            hardware["charge_bypass"] = value
            restored.set()
        self.store.replace({"charge_bypass": True, "controller": controller_backend.DEFAULT_CONTROLLER})
        self.state.update(charge_bypass=True, tdp_profile_pending=True)
        tasks = []
        with patch.object(main, "time", types.SimpleNamespace(monotonic=lambda: clock)), \
             patch.object(main.asyncio, "sleep", side_effect=tick), \
             patch.object(main, "_suspend_clock_offset", return_value=0.0), \
             patch.object(ec_backend, "ac_online", return_value=True), \
             patch.object(power_backend, "apply_tdp", side_effect=apply), \
             patch.object(ec_backend, "read_charge_bypass", side_effect=lambda: hardware["charge_bypass"]), \
             patch.object(ec_backend, "write_charge_bypass", side_effect=charge):
            try:
                tasks.append(asyncio.create_task(self.plugin._ac_loop()))
                # The old implementation monitored charge in _ac_loop only.
                # Run the added independent loop when comparing the fixed code.
                if hasattr(self.plugin, "_charge_loop"):
                    tasks.append(asyncio.create_task(self.plugin._charge_loop()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                hardware["charge_bypass"] = False
                clock = 20.0
                recovered = await asyncio.to_thread(restored.wait, 0.25)
                tdp_still_blocked = not release.is_set()
            finally:
                for task in tasks:
                    task.cancel()
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        self.assertTrue(recovered, "saved charge bypass must recover during a slow TDP retry")
        self.assertTrue(tdp_still_blocked)
        self.assertTrue(hardware["charge_bypass"])

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
