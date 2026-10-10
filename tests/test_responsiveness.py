# SPDX-License-Identifier: BSD-3-Clause
"""RPC responsiveness checks; hardware and services are isolated doubles."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_logic
import main
from safe_settings import SettingsManager


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.controller = dict(main.DEFAULT_CONTROLLER)
        self.store.replace({"controller": self.controller})
        self.state = {"supported": True, "controller": dict(self.controller),
                      "tdp": dict(main.DEFAULT_TDP), "cpu_boost": False,
                      "modules_connected": True, "modules_reconnecting": False,
                      "tdp_supported": True, "tdp_backend": "RyzenAdj",
                      "tdp_error": "", "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "111"))
        self.stack.enter_context(patch.object(main, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(main, "apply_controller"))
        self.stack.enter_context(patch.object(main, "set_vibration_gain"))
        for name, value in (("gpu_power_watts", 3.5),
                            ("tdp_capabilities", {"tdp_backend": "PowerStation",
                             "tdp_supported": False, "tdp_error": "ownership"}),
                            ("_vendor_hidraw", "/fake/hidraw"), ("read_cpu_boost", False),
                            ("_is_our_display_script", False), ("_display_script_conflict", False),
                            ("button_fix_installed", False), ("read_charge_bypass", False),
                            ("module_presence", {"left": True, "right": True})):
            self.stack.enter_context(patch.object(main, name, return_value=value))
        self.stack.enter_context(patch.object(main, "read_published_edid", side_effect=OSError()))
        self.plugin = main.Plugin()

    async def asyncTearDown(self):
        task = self.plugin._tdp_diagnostic_task
        if task is not None:
            await asyncio.wait_for(task, 3)

    async def test_controller_ack_does_not_probe_unrelated_services(self):
        wanted = {**self.controller, "color": "00ff00"}
        with patch.object(main, "tdp_capabilities", side_effect=AssertionError("unrelated probe")), \
             patch.object(main, "module_presence", side_effect=AssertionError("unrelated EC read")):
            response = await self.plugin.set_controller(wanted)
        self.assertEqual(response["controller"], wanted)
        self.assertEqual(self.store.getSetting("controller"), wanted)
        self.assertEqual(response["active_app_id"], "111")
        self.assertNotIn("_applied_tdp", response)
        response["controller"]["color"] = "ffffff"
        self.assertEqual(self.state["controller"]["color"], "00ff00")

    async def test_slow_read_does_not_hold_up_write_ack_or_next_controller_edit(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test failed to release the diagnostic")
            return {"tdp_backend": "RyzenAdj", "tdp_supported": True, "tdp_error": ""}
        tasks = []
        with patch.object(main, "tdp_capabilities", side_effect=probe):
            try:
                tasks.append(asyncio.create_task(self.plugin.get_state()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin.set_controller(
                    {**self.controller, "brightness": 40})))
                tasks.append(asyncio.create_task(self.plugin.set_vibration_gain(20)))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
                acknowledged_before_read = len(done)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(acknowledged_before_read, 2,
                         "both writes must finish while the unrelated read is still blocked")
        self.assertEqual(responses[-1]["controller"]["brightness"], 40)
        self.assertEqual(responses[-1]["controller"]["ff_gain"], 20)

    async def test_fast_ack_preserves_last_observed_capabilities(self):
        await self.plugin.get_state()
        await self.plugin._tdp_diagnostic_task
        observed = await self.plugin.get_state()
        self.assertFalse(observed["tdp_supported"])
        with patch.object(main, "tdp_capabilities", side_effect=AssertionError("redundant probe")):
            response = await self.plugin.set_vibration_gain(20)
        self.assertEqual(response["tdp_backend"], "PowerStation")
        self.assertFalse(response["tdp_supported"])
        self.assertEqual(response["tdp_error"], "ownership")
        self.assertEqual(response["gpu_power_w"], 3.5)
        self.assertEqual(response["controller"]["ff_gain"], 20)

    async def test_diagnostic_publication_cannot_replace_new_game_or_controller_state(self):
        wanted = {**self.controller, "brightness": 30}
        def transition():
            with main._lock:
                main.Plugin._active_app = "222"
                self.state.update(tdp=dict(main.PRESETS["Max"]), controller=wanted,
                                  cpu_boost=True)
            return {"tdp_backend": "RyzenAdj", "tdp_supported": True, "tdp_error": ""}
        with patch.object(main, "tdp_capabilities", side_effect=transition):
            response = await self.plugin._offload(self.plugin._snapshot)
        self.assertEqual(response["active_app_id"], "111")
        self.assertEqual(response["tdp"], main.DEFAULT_TDP)
        self.assertEqual(main.Plugin._active_app, "222")
        self.assertEqual(self.state["tdp"], main.PRESETS["Max"])
        self.assertEqual(self.state["controller"], wanted)
        self.assertTrue(self.state["cpu_boost"])

    async def test_slow_diagnostic_does_not_replace_newer_capability_information(self):
        self.state.update(tdp_supported=False, tdp_error="old failure")
        def recovered():
            with main._lock:
                self.state.update(tdp_supported=True, tdp_error="")
            return {"tdp_backend": "Unavailable", "tdp_supported": False, "tdp_error": "old failure"}
        with patch.object(main, "tdp_capabilities", side_effect=recovered):
            await self.plugin._offload(self.plugin._snapshot)
        self.assertTrue(self.state["tdp_supported"])
        self.assertEqual(self.state["tdp_error"], "")

    async def test_cached_available_tdp_never_bypasses_live_ownership_check(self):
        self.assertTrue(self.state["tdp_supported"])
        with patch.object(main, "_powerstation_active", return_value=True), \
             patch.object(main, "_ensure_ryzenadj") as ensure, \
             patch.object(main.subprocess, "run") as command:
            with self.assertRaises(main.TdpUnavailableError):
                await self.plugin.set_tdp(main.PRESETS["Max"], "Max", "111")
        ensure.assert_not_called()
        command.assert_not_called()
        self.assertEqual(self.state["tdp"], main.DEFAULT_TDP)
        self.assertIsNone(self.store.getSetting("tdp"))
        self.assertFalse(self.state["tdp_supported"], "live rejection must immediately invalidate a cached available status")
        self.assertEqual(self.state["tdp_backend"], "PowerStation")
