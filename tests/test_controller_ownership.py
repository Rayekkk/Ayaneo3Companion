# SPDX-License-Identifier: BSD-3-Clause
"""Before the user changes a setting, Companion keeps the controller's own state."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_logic
import main
import companion_common
import controller_backend
import display_backend
from safe_settings import SettingsManager

FOUND = {"vibration": "low", "ff_gain": 100, "rgb_mode": "pulse", "color": "00ff00", "brightness": 100}


class ControllerOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SettingsManager("settings", str(root / "settings"))
        self.store.replace({})
        self.state = {"supported": True, "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "controller_owned": False, "controller_seeded": False,
                      "modules_connected": True, "modules_reconnecting": False,
                      "modules_detached": False, "tm_guard_enabled": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.read = self.stack.enter_context(patch.object(
            controller_backend, "read_controller", return_value=dict(FOUND)))
        self.reconcile = self.stack.enter_context(patch.object(
            controller_backend, "reconcile_controller", return_value=("healthy", "/dev/hidraw0")))
        self.apply = self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.gain = self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.plugin = main.Plugin()

    def test_monitor_shows_the_controller_state_and_only_guards_tm_mode(self):
        main.Plugin._reconcile_current_controller(True)
        main.Plugin._reconcile_current_controller(True)
        self.read.assert_called_once()
        self.assertEqual(self.state["controller"], FOUND)
        self.assertEqual(self.reconcile.call_args.kwargs, {"repair": False})
        self.assertIsNone(main.Plugin._restore_current_controller())
        self.assertIsNone(main.Plugin._restore_current_vibration_gain())
        self.apply.assert_not_called()
        self.gain.assert_not_called()

    def test_first_change_records_the_found_state_once_and_takes_ownership(self):
        main.Plugin._reconcile_current_controller(True)
        asyncio.run(self.plugin.set_controller({"color": "ff0000"}))
        self.assertEqual(self.store.getSetting("controller_baseline"), FOUND)
        self.assertEqual(self.store.getSetting("controller")["color"], "ff0000")
        self.assertTrue(self.state["controller_owned"])
        asyncio.run(self.plugin.set_vibration_gain(40))
        self.assertEqual(self.store.getSetting("controller_baseline"), FOUND)
        self.assertEqual(self.store.getSetting("controller")["ff_gain"], 40)

    def test_unread_controller_records_no_baseline(self):
        asyncio.run(self.plugin.set_vibration_gain(40))
        self.assertIsNone(self.store.getSetting("controller_baseline", None))
        self.assertTrue(self.state["controller_owned"])

    def test_uninstall_restores_the_recorded_state(self):
        self.store.replace({"controller": dict(controller_backend.DEFAULT_CONTROLLER),
                            "controller_baseline": dict(FOUND, ff_gain=70)})
        for name in ("remove_audio_fix",):
            self.stack.enter_context(patch.object(main.audio_backend, name))
        self.stack.enter_context(patch.object(display_backend, "display_script_owned", return_value=False))
        self.stack.enter_context(patch.object(controller_backend, "program_rear_buttons"))
        self.stack.enter_context(patch.object(main.input_backend, "remove_button_fix"))
        self.stack.enter_context(patch.object(main, "_systemctl"))
        self.stack.enter_context(patch.object(main.fan_controller, "close"))
        asyncio.run(self.plugin._remove_owned())
        self.apply.assert_called_once_with(controller_backend.normalize_controller(dict(FOUND, ff_gain=70)))
        self.gain.assert_called_once_with(70)


class RepairFlagTests(unittest.TestCase):
    def test_without_repair_only_tm_mode_is_undone(self):
        config = dict(controller_backend.DEFAULT_CONTROLLER)
        other = test_logic.controller_report(dict(config, color="00ff00"))
        tm_mode = bytearray(other)
        tm_mode[controller_backend.AYA_CUSTOM_REQUIRED_INDEX] = 1
        for first, expected in ((other, "healthy"), (bytes(tm_mode), "mode_restored")):
            sent = []
            replies = [first, other]

            def exchange(_fd, command, timeout=0.4):
                sent.append(command[4])
                return replies.pop(0) if command == controller_backend.AYA_CHECK else bytes(64)

            with self.subTest(expected=expected), \
                 patch.object(companion_common, "supported_device", return_value=True), \
                 patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
                 patch.object(controller_backend.os, "open", return_value=7), \
                 patch.object(controller_backend.os, "close"), \
                 patch.object(controller_backend.os, "O_NONBLOCK", 0, create=True), \
                 patch.object(controller_backend.time, "sleep"), \
                 patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
                 patch.object(controller_backend, "set_vibration_gain") as gain:
                status, _ = controller_backend.reconcile_controller(config, True, repair=False)
            self.assertEqual(status, expected)
            # No configuration packet (0x21) is ever written without ownership.
            self.assertNotIn(controller_backend.controller_command(config)[4], sent)
            gain.assert_not_called()


class DisplayConflictStateTests(unittest.TestCase):
    def test_replacing_a_foreign_definition_clears_the_conflict_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsManager("settings", directory)
            store.replace({})
            state = {"supported": True, "screen_installed": False, "screen_conflict": True}
            with patch.object(main, "settings", store), \
                 patch.object(main.Plugin, "_state", state), \
                 patch.object(display_backend, "LUA_TARGET", Path(directory) / "aya.lua"), \
                 patch.object(display_backend, "install_display_script"), \
                 patch.object(display_backend, "display_script_conflict", return_value=False), \
                 patch.object(display_backend, "patch_published_edid"):
                result = asyncio.run(main.Plugin().set_screen_fix(True, True))
        self.assertFalse(result["screen_conflict"])
        self.assertTrue(result["screen_installed"])


if __name__ == "__main__":
    unittest.main()
