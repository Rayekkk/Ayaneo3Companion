"""Controller reconnect uses isolated hardware doubles."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_logic  # Install the Decky test harness before importing the backend.
import main
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

if __name__ == "__main__":
    unittest.main()
