# SPDX-License-Identifier: BSD-3-Clause
"""Boot recovery and ayaneo-ec ABI regressions, with no device access."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
if "main" not in sys.modules:
    install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
from safe_settings import SettingsManager


class BootRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store_dir = self.root / "settings"
        self.store = SettingsManager("settings", str(self.store_dir))
        self.store.replace({})
        self.lua = self.root / "etc/gamescope/displays/ayaneo.ayaneo3.oled.lua"
        self.mapping = self.root / "etc/inputplumber/capability_maps.d/ayaneo_type7.yaml"
        self.map_bytes = main.INPUT_MAP_MARKER + b"id: fixture\n"
        for name, value in (
            ("settings", self.store), ("LUA_TARGET", self.lua),
            ("INPUT_MAP_TARGET", self.mapping),
            ("LEGACY_INPUT_MAP_TARGETS", ()),
            ("LEGACY_INPUT_DEVICE_TARGET", self.root / "legacy.yaml"),
        ):
            self.stack.enter_context(mock.patch.object(main, name, value))
        self.stack.enter_context(mock.patch.object(main.Plugin, "_state", {}))
        self.stack.enter_context(mock.patch.object(main.Plugin, "_active_app", ""))
        self.stack.enter_context(mock.patch.object(main, "supported_device", return_value=True))
        self.stack.enter_context(mock.patch.object(main, "_dmi", return_value="AYANEO 3"))
        self.stack.enter_context(mock.patch.object(main, "tdp_backend", return_value="fixture"))
        self.stack.enter_context(mock.patch.object(main, "read_cpu_boost", return_value=True))
        self.stack.enter_context(mock.patch.object(main, "ensure_charge_control", return_value=None))
        self.stack.enter_context(mock.patch.object(main, "ensure_charge_bypass_control", return_value=None))
        self.stack.enter_context(mock.patch.object(main, "ensure_controller_control", return_value=False,
                                                  create=True))
        self.stack.enter_context(mock.patch.object(main, "audio_fix_installed", return_value=False))
        self.stack.enter_context(mock.patch.object(main, "audio_fix_supported", return_value=False))
        self.stack.enter_context(mock.patch.object(main, "_display_script_conflict", return_value=False))
        self.stack.enter_context(mock.patch.object(main, "_display_script_owned",
                                                  side_effect=lambda path: path.exists()))
        self.stack.enter_context(mock.patch.object(main, "_is_our_display_script",
                                                  side_effect=lambda path: path.exists() and
                                                  path.read_bytes().startswith(main.DISPLAY_SCRIPT_MARKER)))
        self.stack.enter_context(mock.patch.object(main, "button_map_owned",
                                                  side_effect=lambda path: path.exists()))
        self.stack.enter_context(mock.patch.object(main, "button_fix_installed",
                                                  side_effect=lambda: self.mapping.exists() and
                                                  self.mapping.read_bytes() == self.map_bytes))
        self.stack.enter_context(mock.patch.object(main.fan_controller, "recover_abandoned"))
        self.stack.enter_context(mock.patch.object(main.updater, "ssl_context"))
        self.systemctl = self.stack.enter_context(mock.patch.object(main, "_systemctl"))
        self.stack.enter_context(mock.patch.object(main, "program_rear_buttons"))
        self.stack.enter_context(mock.patch.object(main, "patch_published_edid", return_value=True))
        self.install_display = self.stack.enter_context(mock.patch.object(
            main, "install_display_script", side_effect=self._install_display))
        self.install_mapping = self.stack.enter_context(mock.patch.object(
            main, "install_button_fix", side_effect=self._install_mapping))
        self.remove_display = self.stack.enter_context(mock.patch.object(
            main, "remove_display_script", side_effect=lambda: self.lua.unlink(missing_ok=True)))
        self.remove_mapping = self.stack.enter_context(mock.patch.object(
            main, "remove_button_fix", side_effect=lambda: self.mapping.unlink(missing_ok=True)))
        self.plugin = main.Plugin()
        # _start normally launches perpetual hardware monitors. They are not
        # needed to verify the boot-time desired-state transaction.
        self.plugin._closing = True

    def _install_display(self, *_args):
        self.lua.parent.mkdir(parents=True, exist_ok=True)
        self.lua.write_bytes(main.DISPLAY_SCRIPT_MARKER + b"-- fixture\n")

    def _install_mapping(self):
        self.mapping.parent.mkdir(parents=True, exist_ok=True)
        self.mapping.write_bytes(self.map_bytes)

    async def test_saved_fixes_return_after_os_replaces_etc(self):
        self.store.replace({"screen_fix_enabled": True, "button_fix_enabled": True})
        self.assertFalse(self.lua.exists())
        self.assertFalse(self.mapping.exists())

        await self.plugin._start()

        self.install_display.assert_called_once()
        self.install_mapping.assert_called_once()
        self.systemctl.assert_any_call("restart", "inputplumber", check=True)
        self.assertTrue(main.Plugin._state["screen_installed"])
        self.assertTrue(main.Plugin._state["button_fix_installed"])
        self.assertTrue(self.store.getSetting("screen_fix_enabled"))
        self.assertTrue(self.store.getSetting("button_fix_enabled"))

    async def _start_restore_flag(self):
        """Run _start with its monitors stubbed; return _restore_hardware's argument."""
        self.plugin._closing = False
        tasks = ("_restore_task", "_edid_task", "_ac_task", "_charge_task", "_module_task",
                 "_tm_guard_task", "_vibration_gain_task", "_audio_task")
        for name in tasks:
            self.stack.enter_context(mock.patch.object(main.Plugin, name, None))
        loops = {name: self.stack.enter_context(mock.patch.object(self.plugin, name, new=mock.AsyncMock()))
                 for name in ("_restore_hardware", "_edid_loop", "_ac_loop", "_charge_loop",
                              "_module_loop", "_tm_guard_loop", "_vibration_gain_loop", "_restore_audio")}
        await self.plugin._start()
        await asyncio.gather(*(getattr(main.Plugin, name) for name in tasks))
        loops["_restore_hardware"].assert_awaited_once()
        return loops["_restore_hardware"].await_args.args[0]

    async def test_reinstalled_key_binding_also_reprograms_rear_buttons(self):
        # Uninstall removed both the map and the LC1/RC1 firmware slots.
        self.store.replace({"button_fix_enabled": True})
        self.assertTrue(await self._start_restore_flag())

    async def test_current_key_binding_leaves_firmware_slots_alone(self):
        self.store.replace({"button_fix_enabled": True})
        self._install_mapping()
        self.assertFalse(await self._start_restore_flag())

    async def test_rear_button_restore_follows_latest_choice(self):
        controller = dict(main.DEFAULT_CONTROLLER)
        for installed, calls in ((True, [mock.call(True, controller)]), (False, [])):
            with self.subTest(installed=installed):
                main.Plugin._state = {"tdp": dict(main.DEFAULT_TDP), "controller": controller,
                                      "button_fix_installed": installed}
                with mock.patch.object(main.asyncio, "sleep", new=mock.AsyncMock()), \
                     mock.patch.object(main.Plugin, "_reapply_current_tdp"), \
                     mock.patch.object(main.Plugin, "_restore_current_controller"), \
                     mock.patch.object(main, "program_rear_buttons") as program:
                    await self.plugin._restore_hardware(True)
                self.assertEqual(program.call_args_list, calls)

    async def test_disabled_or_never_selected_fixes_stay_off(self):
        for preferences in ({}, {"screen_fix_enabled": False, "button_fix_enabled": False}):
            with self.subTest(preferences=preferences):
                self.store.replace(preferences)
                await self.plugin._start()
                self.install_display.assert_not_called()
                self.install_mapping.assert_not_called()
                self.systemctl.assert_not_called()
                self.assertFalse(main.Plugin._state["screen_installed"])
                self.assertFalse(main.Plugin._state["button_fix_installed"])

    async def test_existing_legacy_install_is_migrated_when_preference_is_missing(self):
        self.lua.parent.mkdir(parents=True, exist_ok=True)
        self.lua.write_bytes(b"-- legacy companion display\n")
        self.mapping.parent.mkdir(parents=True, exist_ok=True)
        self.mapping.write_bytes(main.INPUT_MAP_MARKER + b"id: legacy\n")
        await self.plugin._start()
        self.install_display.assert_called_once()
        self.install_mapping.assert_called_once()
        self.assertTrue(main.Plugin._state["screen_installed"])
        self.assertTrue(main.Plugin._state["button_fix_installed"])

    async def test_current_fixes_do_not_reinstall_or_restart_on_plugin_start(self):
        self.store.replace({"screen_fix_enabled": True, "button_fix_enabled": True})
        self._install_display()
        self._install_mapping()
        await self.plugin._start()
        self.install_display.assert_not_called()
        self.install_mapping.assert_not_called()
        self.systemctl.assert_not_called()

    async def test_successful_switches_commit_preference_for_next_boot(self):
        self.plugin._closing = False
        main.Plugin._state = {"supported": True, "controller": dict(main.DEFAULT_CONTROLLER),
                              "screen_installed": False, "button_fix_installed": False}
        with mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={})), \
             mock.patch.object(main, "button_map_owned", side_effect=lambda path: path.exists()):
            await self.plugin.set_screen_fix(True)
            await self.plugin.set_button_fix(True)
        reopened = SettingsManager("settings", str(self.store_dir))
        reopened.read()
        self.assertTrue(reopened.getSetting("screen_fix_enabled"))
        self.assertTrue(reopened.getSetting("button_fix_enabled"))

        with mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={})):
            await self.plugin.set_screen_fix(False)
            await self.plugin.set_button_fix(False)
        reopened.read()
        self.assertFalse(reopened.getSetting("screen_fix_enabled"))
        self.assertFalse(reopened.getSetting("button_fix_enabled"))

    async def test_display_file_is_restored_when_settings_commit_fails(self):
        self.plugin._closing = False
        main.Plugin._state = {"supported": True, "screen_installed": False}
        with mock.patch.object(self.store, "commit", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "previous hardware settings restored"):
                await self.plugin.set_screen_fix(True)
        self.assertFalse(self.lua.exists())
        self.assertIsNone(self.store.getSetting("screen_fix_enabled", None))
        self.assertFalse(main.Plugin._state["screen_installed"])

    async def test_button_map_is_restored_when_settings_commit_fails(self):
        self.plugin._closing = False
        main.Plugin._state = {"supported": True, "controller": dict(main.DEFAULT_CONTROLLER),
                              "button_fix_installed": False}
        with mock.patch.object(self.store, "commit", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "previous bindings restored"):
                await self.plugin.set_button_fix(True)
        self.assertFalse(self.mapping.exists())
        self.assertIsNone(self.store.getSetting("button_fix_enabled", None))
        self.assertFalse(main.Plugin._state["button_fix_installed"])


class NativeControllerAbiTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.native = self.root / "sys/devices/platform/ayaneo-ec"
        self.stack.enter_context(mock.patch.object(main, "AYANEO_EC_ROOT", self.native, create=True))
        self.stack.enter_context(mock.patch.object(main, "supported_device", return_value=True))

    def test_native_controller_abi_does_not_load_raw_ec(self):
        self.native.mkdir(parents=True)
        power = self.native / "controller_power"
        modules = self.native / "controller_modules"
        power.write_text("1\n")
        modules.write_text("both\n")
        with mock.patch.object(main, "ensure_charge_control",
                               side_effect=AssertionError("ec_sys should stay unloaded")) as raw, \
             mock.patch.object(main, "_ec_io_path",
                               side_effect=AssertionError("raw EC should stay unused")), \
             mock.patch.object(main.subprocess, "run",
                               side_effect=AssertionError("modprobe should stay unused")):
            self.assertTrue(main.ensure_controller_control())
            self.assertTrue(main.controller_powered())
            self.assertTrue(main.both_modules_connected())
            self.assertEqual(main.module_presence(), {"left": True, "right": True})
            main.set_controller_power(False)
            self.assertEqual(power.read_text().strip(), "0")
            main.set_controller_power(True)
            self.assertEqual(power.read_text().strip(), "1")
        raw.assert_not_called()

    def test_native_presence_exposes_each_slot(self):
        self.native.mkdir(parents=True)
        (self.native / "controller_power").write_text("1\n")
        modules = self.native / "controller_modules"
        for value, expected in (
            ("left", {"left": True, "right": False}),
            ("right", {"left": False, "right": True}),
            ("none", {"left": False, "right": False}),
        ):
            with self.subTest(value=value):
                modules.write_text(value + "\n")
                self.assertEqual(main.module_presence(), expected)
                self.assertFalse(main.both_modules_connected())

    def test_kernel_616_keeps_raw_ec_fallback(self):
        ec = self.root / "ec"
        data = bytearray(256)
        data[main.EC_CONTROLLER_POWER_REGISTER] = main.EC_CONTROLLER_POWER_ON
        data[main.EC_MODULE_REGISTER] = main.EC_MODULE_RIGHT
        ec.write_bytes(data)
        with mock.patch.object(main, "_ec_io_path", return_value=ec), \
             mock.patch.object(main, "ensure_charge_control", return_value=ec):
            self.assertTrue(main.ensure_controller_control())
            self.assertTrue(main.controller_powered())
            self.assertEqual(main.module_presence(), {"left": True, "right": False})
            main.set_controller_power(False)
        self.assertEqual(ec.read_bytes()[main.EC_CONTROLLER_POWER_REGISTER],
                         main.EC_CONTROLLER_POWER_OFF)

    def test_incomplete_native_driver_fails_closed_without_loading_raw_ec(self):
        self.native.mkdir(parents=True)
        (self.native / "controller_power").write_text("1\n")
        with mock.patch.object(main, "ensure_charge_control",
                               side_effect=AssertionError("must not fall back across active drivers")):
            self.assertFalse(main.ensure_controller_control())


class FanRpcGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_fan_write_rejects_other_devices_even_before_state_is_ready(self):
        with mock.patch.object(main.Plugin, "_state", {}), \
             mock.patch.object(main, "supported_device", return_value=False), \
             mock.patch.object(main.fan_controller, "set") as write:
            with self.assertRaisesRegex(RuntimeError, "restricted to AYANEO 3"):
                await main.Plugin().set_fan("manual", 60)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
