"""Controller reconnect and deferred game profiles use isolated hardware doubles."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_logic  # Install the Decky test harness before importing the backend.
import main
import power_backend
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager


class RoundTwoBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The monitor tests replace monotonic time with a deterministic clock.
        # Avoid asyncio debug timing reports derived from the simulated clock.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SettingsManager("settings", str(root))
        self.base = dict(power_backend.PRESETS["Balanced"])
        self.wanted = dict(power_backend.PRESETS["Max"])
        self.store.replace({"tdp": self.base, "tdp_preset": "Balanced",
                            "game_profiles": {"222": {**self.wanted, "preset": "Max"}}})
        self.state = {"supported": True, "tdp": dict(self.base), "tdp_preset": "Balanced",
                      "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "modules_connected": True, "modules_reconnecting": False,
                      "modules_detached": False}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "111"))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(power_backend, "read_cpu_boost", return_value=False))
        self.stack.enter_context(patch.object(power_backend, "write_cpu_boost"))
        self.stack.enter_context(patch.object(main.Plugin, "get_state", new=AsyncMock(return_value={})))
        self.plugin = main.Plugin()

    async def module_cycle(self, *, eject=False, kernel_owned=False, remain_inserted=False):
        self.state.update(modules_reconnecting=eject, modules_detached=False)
        hardware = {"powered": kernel_owned, "reads": 0, "writes": [], "sleeps": 0}

        async def sleep(_delay):
            hardware["sleeps"] += 1
            if hardware["sleeps"] > 6:
                raise asyncio.CancelledError

        def presence():
            hardware["reads"] += 1
            return {"left": not eject or remain_inserted or hardware["sleeps"] > 1,
                    "right": True}

        def vendor():
            if kernel_owned:
                raise RuntimeError("kernel hid-ayaneo is active; direct controller commands are disabled")
            if not hardware["powered"]:
                raise RuntimeError("AYANEO vendor HID interface not found")
            return "/dev/fake"

        def power(value):
            hardware["writes"].append(value)
            hardware["powered"] = value

        left, right = controller_backend.module_info("left", 1), controller_backend.module_info("right", 1)
        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(controller_backend, "vendor_hidraw", side_effect=vendor), \
             patch.object(ec_backend, "controller_powered", side_effect=lambda: hardware["powered"]), \
             patch.object(ec_backend, "module_presence", side_effect=presence), \
             patch.object(ec_backend, "both_modules_connected", side_effect=lambda: all(presence().values())), \
             patch.object(ec_backend, "set_controller_power", side_effect=power), \
             patch.object(main.Plugin, "_restore_current_controller") as restore, \
             patch.object(controller_backend, "read_module_layout", return_value=(left, right)) as layout:
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._module_loop()
        return hardware, restore, layout

    async def test_eject_reinsert_recovers_without_an_existing_hid_device(self):
        hardware, restore, layout = await self.module_cycle(eject=True)
        self.assertGreater(hardware["reads"], 0)
        self.assertEqual(hardware["writes"], [True])
        self.assertTrue(hardware["powered"])
        self.assertFalse(self.state["modules_reconnecting"])
        self.assertTrue(self.state["modules_connected"])
        restore.assert_called_once()
        layout.assert_called_once()

    async def test_start_with_controller_off_restores_power_before_hid_configuration(self):
        hardware, restore, layout = await self.module_cycle()
        self.assertEqual(hardware["writes"], [True])
        self.assertTrue(hardware["powered"])
        restore.assert_called_once()
        layout.assert_called_once()

    async def test_deliberate_eject_poweroff_is_preserved_until_physical_detach(self):
        hardware, restore, layout = await self.module_cycle(eject=True, remain_inserted=True)
        self.assertEqual(hardware["writes"], [])
        self.assertFalse(hardware["powered"])
        self.assertTrue(self.state["modules_reconnecting"])
        self.assertFalse(self.state["modules_detached"])
        restore.assert_not_called()
        layout.assert_not_called()

    async def test_kernel_owned_hid_still_prevents_raw_controller_transactions(self):
        hardware, restore, layout = await self.module_cycle(kernel_owned=True)
        self.assertGreater(hardware["reads"], 0)
        self.assertEqual(hardware["writes"], [])
        self.assertFalse(self.state["controller_supported"])
        self.assertIn("hid-ayaneo", self.state["controller_error"])
        restore.assert_not_called()
        layout.assert_not_called()

    async def test_blocked_app_tracks_its_own_desired_profile_and_pending_error(self):
        with patch.object(power_backend, "apply_tdp", side_effect=power_backend.TdpUnavailableError("PowerStation active")):
            await self.plugin.set_active_app("222")
        self.assertEqual(main.Plugin._active_app, "222")
        self.assertEqual(self.state["tdp"], self.wanted)
        self.assertEqual(self.state["tdp_preset"], "Max")
        self.assertTrue(self.state["tdp_profile_pending"])
        self.assertIn("PowerStation", self.state["tdp_profile_error"])
        self.assertEqual(self.store.getSetting("tdp"), self.base)

    async def test_same_app_leaves_pending_retry_to_the_power_monitor(self):
        with patch.object(power_backend, "apply_tdp", side_effect=power_backend.TdpUnavailableError("PowerStation active")):
            await self.plugin.set_active_app("222")
        with patch.object(power_backend, "apply_tdp") as apply:
            await self.plugin.set_active_app("222")
            apply.assert_not_called()
            self.assertTrue(self.state["tdp_profile_pending"])
            await self.plugin._offload(main.Plugin._reapply_current_tdp)
        apply.assert_called_once_with(self.wanted)
        self.assertFalse(self.state["tdp_profile_pending"])
        self.assertEqual(self.state["tdp_profile_error"], "")

    async def tdp_monitor(self, *, charger_change):
        clock = 0
        applied = []
        self.state.update(tdp_profile_pending=True, tdp_profile_error="PowerStation active")
        # Emulate the requested profile after a previously blocked app change.
        self.state["tdp"] = dict(self.wanted)
        self.state["_applied_tdp"] = dict(self.base)
        main.Plugin._active_app = "222"

        async def sleep(_delay):
            nonlocal clock
            clock += 1
            if clock > 12:
                raise asyncio.CancelledError

        def apply(value):
            if clock < 4:
                raise power_backend.TdpUnavailableError("PowerStation active")
            applied.append(copy.deepcopy(value))

        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(main.time, "monotonic", side_effect=lambda: float(clock)), \
             patch.object(main, "_suspend_clock_offset", return_value=None), \
             patch.object(ec_backend, "ac_online", side_effect=lambda: charger_change and clock > 2), \
             patch.object(power_backend, "apply_tdp", side_effect=apply), \
             patch.object(main.Plugin, "_restore_saved_control"):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._ac_loop()
        return applied

    async def test_pending_profile_resumes_without_an_app_rpc_or_ac_event(self):
        applied = await self.tdp_monitor(charger_change=False)
        self.assertGreater(len(applied), 0)
        self.assertTrue(all(value == self.wanted for value in applied))
        self.assertFalse(self.state["tdp_profile_pending"])
        self.assertEqual(self.state["tdp_profile_error"], "")

    async def test_ac_reapply_uses_pending_current_game_profile_and_clears_error(self):
        applied = await self.tdp_monitor(charger_change=True)
        self.assertGreater(len(applied), 0)
        self.assertTrue(all(value == self.wanted for value in applied))
        self.assertFalse(self.state["tdp_profile_pending"])
        self.assertEqual(self.state["tdp_profile_error"], "")

    async def test_generic_profile_failure_keeps_current_app_and_retries_after_rollback(self):
        calls = []
        def apply(value):
            calls.append(copy.deepcopy(value))
            if len(calls) == 1:
                raise RuntimeError("SMU temporarily rejected the request")
        with patch.object(power_backend, "apply_tdp", side_effect=apply):
            with self.assertRaisesRegex(RuntimeError, "SMU temporarily"):
                await self.plugin.set_active_app("222")
            self.assertEqual(main.Plugin._active_app, "222")
            self.assertEqual(self.state["tdp"], self.wanted)
            self.assertTrue(self.state["tdp_profile_pending"])
            await self.plugin.set_active_app("222")
            self.assertTrue(self.state["tdp_profile_pending"])
            await self.plugin._offload(main.Plugin._reapply_current_tdp)
        self.assertEqual(calls, [self.wanted, self.base, self.wanted])
        self.assertFalse(self.state["tdp_profile_pending"])

    async def test_profile_failure_rolls_back_to_the_latest_successful_game(self):
        second = {"spl": 20, "sppt": 23, "fppt": 28}
        third = {"spl": 25, "sppt": 28, "fppt": 32}
        self.store.replace({"tdp": self.base, "game_profiles": {
            "222": self.wanted, "333": second, "444": third}})
        calls = []
        def apply(value):
            calls.append(copy.deepcopy(value))
            if value == third:
                raise RuntimeError("third game rejected")
        with patch.object(power_backend, "apply_tdp", side_effect=apply):
            await self.plugin.set_active_app("222")
            await self.plugin.set_active_app("333")
            with self.assertRaisesRegex(RuntimeError, "third game rejected"):
                await self.plugin.set_active_app("444")
        self.assertEqual(calls, [self.wanted, second, third, second])
        self.assertEqual(main.Plugin._active_app, "444")
        self.assertEqual(self.state["tdp"], third)
        self.assertEqual(self.state["_applied_tdp"], second)
        self.assertTrue(self.state["tdp_profile_pending"])

    async def test_global_game_and_delete_writes_track_actual_applied_limits(self):
        custom = {"spl": 18, "sppt": 21, "fppt": 29}
        with patch.object(power_backend, "apply_tdp"):
            await self.plugin.set_tdp(custom, "Custom", "111")
            self.assertEqual(self.state["_applied_tdp"], custom)
            await self.plugin.set_game_profile("111", self.wanted, "Max", "111")
            self.assertEqual(self.state["_applied_tdp"], self.wanted)
            await self.plugin.delete_game_profile("111", "111")
            self.assertEqual(self.state["_applied_tdp"], custom)
        self.assertFalse(self.state["tdp_profile_pending"])

    async def test_private_applied_limits_do_not_leak_in_public_snapshot(self):
        self.state.update(initializing=True, _applied_tdp=self.wanted)
        snapshot = main.Plugin._snapshot()
        self.assertNotIn("_applied_tdp", snapshot)
        self.assertEqual(snapshot["tdp"], self.base)
        self.assertEqual(self.state["_applied_tdp"], self.wanted)


if __name__ == "__main__":
    unittest.main()
