# SPDX-License-Identifier: BSD-3-Clause
"""Regressions for cross-device controller commits and truthful capabilities."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_logic  # Install the isolated Decky harness before importing main.
import main
from safe_settings import SettingsManager


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SettingsManager("settings", str(self.root))
        self.previous = dict(main.DEFAULT_CONTROLLER)
        self.wanted = {**self.previous, "color": "00ff00", "ff_gain": 60}
        self.store.replace({"controller": self.previous, "tdp": dict(main.DEFAULT_TDP)})
        self.hardware = {"firmware": dict(self.previous), "gain": self.previous["ff_gain"]}
        state = {"supported": True, "controller": dict(self.previous), "tdp": dict(main.DEFAULT_TDP)}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "111"))
        self.stack.enter_context(patch.object(main, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(main, "_powerstation_active", return_value=False))
        self.stack.enter_context(patch.object(main.Plugin, "get_state", new=AsyncMock(return_value={})))
        self.firmware = self.stack.enter_context(patch.object(main, "apply_controller", side_effect=self.apply_firmware))
        self.gain = self.stack.enter_context(patch.object(main, "set_vibration_gain", side_effect=self.apply_gain))
        self.plugin = main.Plugin()

    def apply_firmware(self, value, *_args, **_kwargs):
        self.hardware["firmware"] = dict(value)

    def apply_gain(self, value):
        self.hardware["gain"] = value

    def assert_controller_restored(self):
        self.assertEqual(main.Plugin._state["controller"], self.previous)
        self.assertEqual(self.store.getSetting("controller"), self.previous)
        self.assertEqual(SettingsManager("settings", str(self.root)).getSetting("controller"), self.previous)
        self.assertEqual(self.hardware["firmware"], self.previous)
        self.assertEqual(self.hardware["gain"], self.previous["ff_gain"])

    def test_combined_controller_change_commits_once_after_both_devices(self):
        commit = self.store.commit
        def verify():
            self.assertEqual(self.hardware["firmware"], self.wanted)
            self.assertEqual(self.hardware["gain"], 60)
            self.assertEqual(main.Plugin._state["controller"], self.previous)
            commit()
        with patch.object(self.store, "commit", side_effect=verify) as saved:
            asyncio.run(self.plugin.set_controller(self.wanted))
        saved.assert_called_once()
        self.gain.assert_called_once_with(60)
        self.assertEqual(main.Plugin._state["controller"], self.wanted)

    def test_gain_failure_restores_actual_previous_gain_and_rgb_without_saving(self):
        calls = []
        def fail(value):
            calls.append(value)
            self.apply_gain(value)
            if len(calls) == 1:
                raise OSError("evdev write failed after applying")
        self.gain.side_effect = fail
        with patch.object(self.store, "commit") as saved, self.assertRaisesRegex(RuntimeError, "restored"):
            asyncio.run(self.plugin.set_controller(self.wanted))
        saved.assert_not_called()
        self.assertEqual(calls, [60, 100])
        self.assert_controller_restored()

    def test_commit_failure_restores_firmware_and_gain(self):
        with patch.object(self.store, "commit", side_effect=OSError("disk full")), self.assertRaisesRegex(RuntimeError, "restored"):
            asyncio.run(self.plugin.set_controller(self.wanted))
        self.assertEqual([call.args[0] for call in self.gain.call_args_list], [60, 100])
        self.assert_controller_restored()

    def test_firmware_rollback_failure_still_attempts_old_gain(self):
        self.firmware.side_effect = OSError("HID unavailable")
        with self.assertRaisesRegex(RuntimeError, "hardware rollback failed"):
            asyncio.run(self.plugin.set_controller(self.wanted))
        self.gain.assert_called_once_with(100)
        self.assertEqual(self.store.getSetting("controller"), self.previous)

    def test_feedback_rpc_uses_same_combined_transaction(self):
        asyncio.run(self.plugin.set_controller_with_vibration_feedback(self.wanted))
        self.firmware.assert_called_once_with(self.wanted, True, self.previous["vibration"], False)
        self.gain.assert_called_once_with(60)
        self.assertEqual(self.store.getSetting("controller"), self.wanted)

    def test_unchanged_gain_does_not_require_an_evdev_device(self):
        asyncio.run(self.plugin.set_controller({**self.previous, "color": "00ff00"}))
        self.gain.assert_not_called()

    def test_powerstation_rpc_rejects_without_save_or_boost_rollback(self):
        with patch.object(main, "_powerstation_active", return_value=True), \
             patch.object(main, "read_cpu_boost", return_value=True), \
             patch.object(main, "write_cpu_boost") as boost, \
             patch.object(self.store, "commit") as saved:
            with self.assertRaises(main.TdpUnavailableError):
                asyncio.run(self.plugin.set_tdp(main.PRESETS["Max"], expected_app_id="111"))
        saved.assert_not_called()
        boost.assert_not_called()
        self.assertEqual(main.Plugin._state["tdp"], main.DEFAULT_TDP)

    def test_app_tracking_advances_when_powerstation_owns_tdp(self):
        with patch.object(main, "_powerstation_active", return_value=True), \
             patch.object(main, "read_cpu_boost", return_value=True), \
             patch.object(main, "write_cpu_boost") as boost:
            asyncio.run(self.plugin.set_active_app("222"))
        self.assertEqual(main.Plugin._active_app, "222")
        self.assertFalse(main.Plugin._state["tdp_supported"])
        boost.assert_not_called()

    def test_boot_does_not_retry_an_unsupported_tdp_writer_forever(self):
        with patch.object(main.asyncio, "sleep", new=AsyncMock()), \
             patch.object(main.Plugin, "_reapply_current_tdp", side_effect=main.TdpUnavailableError("PowerStation")) as tdp, \
             patch.object(main.Plugin, "_restore_current_controller"):
            asyncio.run(self.plugin._restore_hardware())
        tdp.assert_called_once()

    def snapshot_fixture(self):
        for name, value in (("gpu_power_watts", None), ("tdp_backend", "RyzenAdj"),
                            ("_vendor_hidraw", "/dev/fake"), ("read_cpu_boost", True),
                            ("_is_our_display_script", False), ("_display_script_conflict", False),
                            ("button_fix_installed", False), ("read_charge_bypass", False),
                            ("module_presence", {"left": True, "right": True})):
            self.stack.enter_context(patch.object(main, name, return_value=value))
        self.stack.enter_context(patch.object(main, "read_published_edid", side_effect=OSError("no display")))

    def test_hid_unavailable_or_kernel_owned_disables_module_actions_with_reason(self):
        self.snapshot_fixture()
        for error in ("AYANEO vendor HID interface not found", "kernel hid-ayaneo is active"):
            with self.subTest(error=error), patch.object(main, "_vendor_hidraw", side_effect=RuntimeError(error)):
                state = main.Plugin._snapshot()
                self.assertFalse(state["controller_supported"])
                self.assertFalse(state["module_eject_supported"])
                self.assertFalse(state["module_reset_supported"])
                self.assertEqual(state["controller_error"], error)
                self.assertEqual(state["module_left"]["label"], "Unavailable")
        state = main.Plugin._snapshot()
        self.assertTrue(state["controller_supported"])
        self.assertTrue(state["module_eject_supported"])
        self.assertEqual(state["controller_error"], "")

    def test_snapshot_pairs_app_identity_with_its_tdp_before_slow_reads(self):
        self.snapshot_fixture()
        def transition():
            main.Plugin._active_app = "222"
            main.Plugin._state["tdp"] = dict(main.PRESETS["Max"])
        with patch.object(main, "gpu_power_watts", side_effect=transition):
            state = main.Plugin._snapshot()
        self.assertEqual(state["active_app_id"], "111")
        self.assertEqual(state["tdp"], main.DEFAULT_TDP)

    def test_module_monitor_reports_blocked_hid_and_recovers_when_it_returns(self):
        observed = []
        sleeps = 0
        async def sleep(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                observed.append(copy.deepcopy(main.Plugin._state))
            if sleeps > 2:
                raise asyncio.CancelledError
        left = main._module_info("left", 0)
        right = main._module_info("right", 0)
        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(main, "_vendor_hidraw", side_effect=[RuntimeError("kernel hid-ayaneo is active"), "/dev/fake"]), \
             patch.object(main, "controller_powered", return_value=True), \
             patch.object(main, "module_presence", return_value={"left": True, "right": True}), \
             patch.object(main, "set_controller_power") as power, \
             patch.object(main, "read_module_layout", return_value=(left, right)) as layout:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.plugin._module_loop())
        self.assertEqual(observed[0]["module_left"]["label"], "Unavailable")
        self.assertIn("hid-ayaneo", observed[0]["controller_error"])
        self.assertFalse(observed[0]["controller_supported"])
        self.assertEqual(main.Plugin._state["module_left"], left)
        self.assertTrue(main.Plugin._state["controller_supported"])
        self.assertEqual(main.Plugin._state["controller_error"], "")
        layout.assert_called_once()
        power.assert_not_called()

    def test_snapshot_reports_powerstation_limits_as_unavailable(self):
        self.snapshot_fixture()
        with patch.object(main, "tdp_backend", return_value="PowerStation"):
            state = main.Plugin._snapshot()
        self.assertFalse(state["tdp_supported"])
        self.assertIn("37 W", state["tdp_error"])

    def test_unavailable_tdp_probe_does_not_hide_independent_controls(self):
        self.snapshot_fixture()
        with patch.object(main, "tdp_backend", side_effect=RuntimeError("ownership probe unavailable")):
            state = main.Plugin._snapshot()
        self.assertFalse(state["tdp_supported"])
        self.assertEqual(state["tdp_error"], "ownership probe unavailable")
        self.assertTrue(state["cpu_boost_supported"])
        self.assertTrue(state["controller_supported"])

    def test_update_rpc_passes_selected_version_and_rejects_no_selection(self):
        updater = Mock()
        updater.download.return_value = {"success": False, "error": "latest release changed"}
        with patch.object(main, "updater", updater):
            result = asyncio.run(self.plugin.perform_update("1.2.1"))
            self.assertFalse(result["success"])
            updater.download.assert_called_once_with("1.2.1")
            with self.assertRaises(ValueError):
                asyncio.run(self.plugin.perform_update(None))
            updater.download_latest.assert_not_called()
