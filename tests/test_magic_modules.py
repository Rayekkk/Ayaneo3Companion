"""Controller reconnect uses isolated hardware doubles."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import os
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
import main
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager


class ModuleReinsertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # The monitor tests replace monotonic time with a deterministic clock.
        # Avoid asyncio debug timing reports derived from the simulated clock.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.store = SettingsManager("settings", str(root))
        self.store.replace({})
        self.state = {"supported": True,
                      "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "modules_connected": True, "modules_reconnecting": False,
                      "modules_detached": False}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
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


class MagicModuleTests(unittest.TestCase):
    def test_magic_module_eject_commands(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        self.assertEqual(controller_backend.controller_command(config, "left")[20], 0x07)
        self.assertEqual(controller_backend.controller_command(config, "right")[20], 0x70)
        self.assertEqual(controller_backend.controller_command(config, "both")[20], 0x77)

    def test_magic_module_eject_timeout_keeps_controller_powered(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        busy = bytearray(64)
        busy[19] = 0x02
        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", return_value=bytes(busy)), \
             mock.patch.object(main.time, "sleep"), \
             mock.patch.object(ec_backend, "set_controller_power") as set_power:
            with self.assertRaisesRegex(TimeoutError, "controller power was left on"):
                controller_backend.eject_controller_modules("both", config)
        set_power.assert_not_called()

    def test_module_eject_blocks_background_recovery_before_hardware_command(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": False,
            "modules_connected": True,
            "modules_detached": False,
            "module_left": controller_backend.module_info("left"),
            "module_right": controller_backend.module_info("right"),
        }

        def eject(_side, _controller):
            self.assertTrue(main.Plugin._state["modules_reconnecting"])
            self.assertFalse(main.Plugin._state["modules_connected"])
            self.assertEqual(main.Plugin._state["module_right"]["status"], "ejecting")

        try:
            with mock.patch.object(controller_backend, "eject_controller_modules", side_effect=eject), \
                 mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={})):
                asyncio.run(main.Plugin().eject_modules("right"))
            self.assertTrue(main.Plugin._state["modules_reconnecting"])
        finally:
            main.Plugin._state = previous_state

    def test_module_monitor_does_not_undo_eject_poweroff_before_detach(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": True,
            "modules_connected": False,
            "modules_detached": False,
            "module_left": controller_backend.module_info("left", status="ejecting"),
            "module_right": controller_backend.module_info("right", status="ejecting"),
        }
        sleeps = 0

        async def stop_after_one_iteration(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        try:
            with mock.patch.object(main.asyncio, "sleep", side_effect=stop_after_one_iteration), \
                 mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
                 mock.patch.object(ec_backend, "controller_powered", return_value=False), \
                 mock.patch.object(ec_backend, "module_presence", return_value={"left": True, "right": True}), \
                 mock.patch.object(ec_backend, "set_controller_power") as set_power, \
                 mock.patch.object(controller_backend, "apply_controller") as apply:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._module_loop())
            set_power.assert_not_called()
            apply.assert_not_called()
        finally:
            main.Plugin._state = previous_state

    def test_magic_module_presence_matches_ec_absent_bits(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(ec_backend, "AYANEO_EC_ROOT", Path(directory) / "absent"):
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=0):
                self.assertEqual(ec_backend.module_presence(), {"left": True, "right": True})
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=ec_backend.EC_MODULE_LEFT):
                self.assertEqual(ec_backend.module_presence(), {"left": False, "right": True})
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=ec_backend.EC_MODULE_RIGHT):
                self.assertEqual(ec_backend.module_presence(), {"left": True, "right": False})

    def test_magic_module_identification_and_layout(self):
        response = bytearray(64)
        response[controller_backend.MODULE_INFO_LEFT_INDEX] = 0x44
        response[controller_backend.MODULE_INFO_RIGHT_INDEX] = 0x50
        left, right = controller_backend.decode_module_layout(bytes(response))
        self.assertEqual((left["label"], left["layout"]),
                         ("Joystick / Cross", "Top: Joystick · Bottom: Cross"))
        self.assertEqual((right["label"], right["layout"]),
                         ("Joystick / ABXY", "Top: ABXY · Bottom: Joystick"))

    def test_incomplete_module_pair_uses_unpowered_state(self):
        left, right = controller_backend.module_states_from_presence({"left": True, "right": False})
        self.assertEqual((left["status"], right["status"]), ("unpowered", "disconnected"))

    def test_magic_module_quick_reset_packet(self):
        config = {"vibration": "high", "rgb_mode": "solid", "color": "ff0000", "brightness": 100}
        self.assertEqual(controller_backend.controller_command(config, reset=True)[20], 0x88)

    def test_startup_restore_never_activates_magic_module_reset(self):
        previous = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "button_fix_installed": True,
        }
        try:
            with mock.patch.object(main.settings, "getSetting", return_value=None), \
                 mock.patch.object(main.asyncio, "sleep", new=mock.AsyncMock()), \
                 mock.patch.object(controller_backend, "apply_controller"), \
                 mock.patch.object(controller_backend, "set_vibration_gain"), \
                 mock.patch.object(controller_backend, "program_rear_buttons") as program:
                asyncio.run(main.Plugin()._restore_hardware())
            program.assert_not_called()
        finally:
            main.Plugin._state = previous

    def test_unload_does_not_undo_an_in_progress_module_eject(self):
        previous_state = main.Plugin._state
        task_names = (
            "_audio_task", "_restore_task", "_edid_task", "_charge_task",
            "_module_task", "_tm_guard_task", "_vibration_gain_task",
        )
        previous_tasks = {name: getattr(main.Plugin, name) for name in task_names}
        main.Plugin._state = {"supported": True, "modules_reconnecting": True}
        for name in task_names:
            setattr(main.Plugin, name, None)
        try:
            with mock.patch.object(companion_common, "supported_device") as supported, \
                 mock.patch.object(ec_backend, "both_modules_connected") as connected, \
                 mock.patch.object(ec_backend, "controller_powered") as powered, \
                 mock.patch.object(ec_backend, "set_controller_power") as set_power:
                asyncio.run(main.Plugin()._unload())
            supported.assert_not_called()
            connected.assert_not_called()
            powered.assert_not_called()
            set_power.assert_not_called()
        finally:
            main.Plugin._state = previous_state
            for name, value in previous_tasks.items():
                setattr(main.Plugin, name, value)


if __name__ == "__main__":
    unittest.main()
