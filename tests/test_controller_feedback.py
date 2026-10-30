# SPDX-License-Identifier: BSD-3-Clause
"""Regressions for cross-device controller commits and truthful capabilities."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support  # noqa: F401  (the Decky harness before any backend import)
from support import owner
import main
import display_backend
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager


class ControllerFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SettingsManager("settings", str(self.root))
        self.previous = dict(controller_backend.DEFAULT_CONTROLLER)
        self.wanted = {**self.previous, "color": "00ff00", "ff_gain": 60}
        self.store.replace({"controller": self.previous})
        self.hardware = {"firmware": dict(self.previous), "gain": self.previous["ff_gain"]}
        state = {"supported": True, "controller": dict(self.previous)}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(main.Plugin, "get_state", new=AsyncMock(return_value={})))
        self.firmware = self.stack.enter_context(patch.object(controller_backend, "apply_controller", side_effect=self.apply_firmware))
        self.gain = self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain", side_effect=self.apply_gain))
        self.plugin = main.Plugin()

    def apply_firmware(self, value, *_args, **_kwargs):
        self.hardware["firmware"] = dict(value)

    def apply_gain(self, value):
        self.hardware["gain"] = value

    def assert_refused(self, result, pattern="."):
        support.assert_refused(self, result, pattern)

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
            self.assertIs(asyncio.run(self.plugin.set_controller(self.wanted))["ok"], True)
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
        with patch.object(self.store, "commit") as saved:
            self.assert_refused(asyncio.run(self.plugin.set_controller(self.wanted)), "restored")
        saved.assert_not_called()
        self.assertEqual(calls, [60, 100])
        self.assert_controller_restored()

    def test_commit_failure_restores_firmware_and_gain(self):
        with patch.object(self.store, "commit", side_effect=OSError("disk full")):
            self.assert_refused(asyncio.run(self.plugin.set_controller(self.wanted)), "restored")
        self.assertEqual([call.args[0] for call in self.gain.call_args_list], [60, 100])
        self.assert_controller_restored()

    def test_firmware_rollback_failure_still_attempts_old_gain(self):
        self.firmware.side_effect = OSError("HID unavailable")
        self.assert_refused(asyncio.run(self.plugin.set_controller(self.wanted)), "hardware rollback failed")
        self.gain.assert_called_once_with(100)
        self.assertEqual(self.store.getSetting("controller"), self.previous)

    def test_feedback_rpc_uses_same_combined_transaction(self):
        self.assertIs(asyncio.run(self.plugin.set_controller_with_vibration_feedback(self.wanted))["ok"], True)
        self.firmware.assert_called_once_with(self.wanted, True, self.previous["vibration"], False)
        self.gain.assert_called_once_with(60)
        self.assertEqual(self.store.getSetting("controller"), self.wanted)

    def test_unchanged_gain_does_not_require_an_evdev_device(self):
        self.assertIs(asyncio.run(self.plugin.set_controller({**self.previous, "color": "00ff00"}))["ok"], True)
        self.gain.assert_not_called()

    def snapshot_fixture(self):
        for name, value in (("vendor_hidraw", "/dev/fake"),
                            ("is_our_display_script", False), ("display_script_conflict", False),
                            ("button_fix_installed", False), ("read_charge_bypass", False),
                            ("module_presence", {"left": True, "right": True})):
            self.stack.enter_context(patch.object(owner(name), name, return_value=value))
        self.stack.enter_context(patch.object(display_backend, "read_published_edid", side_effect=OSError("no display")))

    def test_hid_unavailable_or_kernel_owned_disables_module_actions_with_reason(self):
        self.snapshot_fixture()
        for error in ("AYANEO vendor HID interface not found", "kernel hid-ayaneo is active"):
            with self.subTest(error=error), patch.object(controller_backend, "vendor_hidraw", side_effect=RuntimeError(error)):
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
        left = controller_backend.module_info("left", 0)
        right = controller_backend.module_info("right", 0)
        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(controller_backend, "vendor_hidraw", side_effect=[RuntimeError("kernel hid-ayaneo is active"), "/dev/fake"]), \
             patch.object(ec_backend, "controller_powered", return_value=True), \
             patch.object(ec_backend, "module_presence", return_value={"left": True, "right": True}), \
             patch.object(ec_backend, "set_controller_power") as power, \
             patch.object(controller_backend, "read_module_layout", return_value=(left, right)) as layout:
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

    def test_version_names_the_home_folder_for_the_panels_short_paths(self):
        with patch.object(companion_common, "plugin_version", return_value="1.3.0"):
            result = asyncio.run(self.plugin.get_version())
        self.assertEqual(result, {"ok": True, "version": "1.3.0", "home": main.decky.DECKY_USER_HOME})

    def test_update_rpc_passes_selected_version_and_rejects_no_selection(self):
        updates = Mock()
        updates.download.return_value = {"ok": False, "error": "latest release changed"}
        self.plugin._updates = updates
        result = asyncio.run(self.plugin.perform_update("1.2.1"))
        self.assertEqual(result, {"ok": False, "error": "latest release changed"})
        updates.download.assert_called_once_with("1.2.1")
        self.assert_refused(asyncio.run(self.plugin.perform_update(None)), "Select a release version")
        updates.download.assert_called_once_with("1.2.1")
