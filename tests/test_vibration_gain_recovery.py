# SPDX-License-Identifier: BSD-3-Clause
"""Evdev gain recovery uses fake device incarnations, never real HID or input IO."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_logic  # Install the Decky import harness before loading the backend.
from _harness import owner
import main
import controller_backend
import companion_common
from safe_settings import SettingsManager


TOKEN = ("/dev/input/event5", 2, 20)
TASK_NAMES = ("_restore_task", "_edid_task", "_charge_task", "_module_task",
              "_tm_guard_task", "_vibration_gain_task", "_audio_task")


class VibrationGainRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.state = {"supported": True, "controller": {**controller_backend.DEFAULT_CONTROLLER, "ff_gain": 60},
                      "modules_connected": True, "modules_reconnecting": False,
                      "tm_guard_enabled": False, "tm_guard_recoveries": 0}
        self.store.replace({"controller": dict(self.state["controller"]), "audio_fix_enabled": False})
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(main, "_suspend_clock_offset", return_value=None))
        self.hid = self.stack.enter_context(patch.object(controller_backend, "vendor_hidraw",
            side_effect=RuntimeError("kernel hid-ayaneo is active; direct commands are disabled")))
        self.raw = self.stack.enter_context(patch.object(controller_backend, "_hid_exchange",
            side_effect=AssertionError("gain recovery must not issue raw HID commands")))
        self.hardware = {"gain": 100}
        self.writes = []
        self.plugin = main.Plugin()

    def write_gain(self, value):
        self.writes.append(value)
        self.hardware["gain"] = value

    async def cycles(self, tokens, *, before=None, writer=None):
        iteration = 0
        async def sleep(_delay):
            nonlocal iteration
            iteration += 1
            if iteration > len(tokens):
                raise asyncio.CancelledError
            if before:
                before(iteration)
        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(controller_backend, "rumble_event_token", side_effect=tokens) as discovery, \
             patch.object(controller_backend, "set_vibration_gain", side_effect=writer or self.write_gain):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._vibration_gain_loop()
        self.raw.assert_not_called()
        return discovery

    async def test_startup_restores_saved_gain_with_blocked_or_missing_hid_and_either_tm_policy(self):
        for error in ("kernel hid-ayaneo is active", "AYANEO vendor HID interface not found"):
            self.hid.side_effect = RuntimeError(error)
            for enabled in (False, True):
                for gain in (0, 60, 100):
                    with self.subTest(error=error, tm_guard=enabled, gain=gain):
                        self.state["controller"]["ff_gain"] = gain
                        self.state["tm_guard_enabled"] = enabled
                        self.store.replace({"controller": dict(self.state["controller"])})
                        self.writes.clear()
                        self.hardware["gain"] = 100
                        await self.cycles([TOKEN] * 3)
                        self.assertEqual(self.writes, [gain])
                        self.assertEqual(self.hardware["gain"], gain)
                        self.assertEqual(self.store.getSetting("controller")["ff_gain"], gain)
        self.hid.assert_not_called()

    async def test_reused_event_name_with_new_inode_restores_gain_again(self):
        def recreate(iteration):
            if iteration == 2:
                self.hardware["gain"] = 100
        await self.cycles([TOKEN, (TOKEN[0], TOKEN[1], 21), (TOKEN[0], TOKEN[1], 21)], before=recreate)
        self.assertEqual(self.writes, [60, 60])
        self.assertEqual(self.hardware["gain"], 60)

    async def test_missing_evdev_device_waits_and_restores_when_it_appears(self):
        await self.cycles([None, None, TOKEN, TOKEN])
        self.assertEqual(self.writes, [60])
        self.assertEqual(self.hardware["gain"], 60)

    async def test_failed_write_retries_same_device_with_latest_saved_gain(self):
        attempts = []
        def write(value):
            attempts.append(value)
            if len(attempts) == 1:
                raise OSError("evdev temporarily unavailable")
            self.write_gain(value)
        def change_gain(iteration):
            if iteration == 2:
                self.state["controller"]["ff_gain"] = 0
                self.store.replace({"controller": dict(self.state["controller"])})
        await self.cycles([TOKEN] * 3, before=change_gain, writer=write)
        self.assertEqual(attempts, [60, 0])
        self.assertEqual(self.hardware["gain"], 0)

    async def test_resume_with_unchanged_node_retries_failed_restore(self):
        attempts = []
        def write(value):
            attempts.append(value)
            if len(attempts) == 2:
                raise OSError("evdev write failed during resume")
            self.write_gain(value)
        def resume(iteration):
            if iteration == 2:
                self.hardware["gain"] = 100
        with patch.object(main, "_suspend_clock_offset", side_effect=[0.0, 0.0, 5.0, 5.0]):
            await self.cycles([TOKEN] * 3, before=resume, writer=write)
        self.assertEqual(attempts, [60, 60, 60])
        self.assertEqual(self.hardware["gain"], 60)

    async def test_gain_restore_waits_for_eject_and_module_reconnection(self):
        def modules(iteration):
            self.state.update(modules_reconnecting=iteration == 2,
                              modules_connected=iteration != 3)
        discovery = await self.cycles([TOKEN] * 4, before=modules)
        self.assertEqual(discovery.call_count, 2)
        self.assertEqual(self.writes, [60, 60])

    async def test_queued_gain_restore_rechecks_module_operation_under_lock(self):
        entered = threading.Event()
        errors = []
        def worker():
            entered.set()
            try:
                main.Plugin._restore_current_vibration_gain()
            except RuntimeError as error:
                errors.append(str(error))
        with patch.object(controller_backend, "set_vibration_gain") as gain:
            with controller_backend.controller_apply_lock:
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.state["modules_reconnecting"] = True
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["Waiting for controller modules"])
        gain.assert_not_called()

    async def test_queued_gain_restore_reads_latest_value_after_slider_commit(self):
        entered = threading.Event()
        results = []
        def worker():
            entered.set()
            results.append(main.Plugin._restore_current_vibration_gain())
        with patch.object(controller_backend, "set_vibration_gain", side_effect=self.write_gain):
            with controller_backend.controller_apply_lock:
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(entered.wait(2))
                self.state["controller"]["ff_gain"] = 0
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [0])
        self.assertEqual(self.writes, [0])

    async def test_shutdown_cancels_gain_monitor_and_drains_its_inflight_write(self):
        entered, release = threading.Event(), threading.Event()
        real_sleep = asyncio.sleep
        def write(value):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release the fake evdev write")
            self.write_gain(value)
        with ExitStack() as stack:
            for name in TASK_NAMES:
                stack.enter_context(patch.object(main.Plugin, name, None))
            stack.enter_context(patch.object(main, "CONTROLLER_GAIN_CHECK_INTERVAL", 0.001))
            stack.enter_context(patch.object(controller_backend, "rumble_event_token", return_value=TOKEN))
            stack.enter_context(patch.object(controller_backend, "set_vibration_gain", side_effect=write))
            stack.enter_context(patch.object(companion_common, "supported_device", return_value=False))
            stack.enter_context(patch.object(companion_common, "updater", types.SimpleNamespace(close=Mock())))
            monitor = asyncio.create_task(self.plugin._vibration_gain_loop())
            main.Plugin._vibration_gain_task = monitor
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                unloading = asyncio.create_task(self.plugin._unload())
                await real_sleep(0.01)
                self.assertFalse(unloading.done())
                release.set()
                await asyncio.wait_for(unloading, 2)
                self.assertTrue(monitor.cancelled())
                self.assertIsNone(main.Plugin._vibration_gain_task)
                self.assertFalse(self.plugin._workers)
                self.assertEqual(self.writes, [60])
            finally:
                release.set()
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)

    async def test_startup_registers_independent_gain_task(self):
        with ExitStack() as stack:
            for name in TASK_NAMES:
                stack.enter_context(patch.object(main.Plugin, name, None))
            for name in ("button_map_owned", "button_fix_installed", "display_script_owned",
                         "is_our_display_script", "display_script_conflict", "audio_fix_supported"):
                stack.enter_context(patch.object(owner(name), name, return_value=False))
            for name in ("ensure_controller_control", "ensure_charge_bypass_control"):
                stack.enter_context(patch.object(owner(name), name, return_value=None))
            stack.enter_context(patch.object(companion_common, "dmi", return_value="AYANEO 3"))
            stack.enter_context(patch.object(self.plugin, "_start_tdp", new=AsyncMock()))
            stack.enter_context(patch.object(companion_common, "updater", types.SimpleNamespace(
                ssl_context=Mock(), plugin_version=Mock(return_value="1.2.0"))))
            for name in ("_restore_hardware", "_edid_loop", "_charge_loop", "_module_loop", "_tm_guard_loop", "_restore_audio"):
                stack.enter_context(patch.object(self.plugin, name, new=AsyncMock()))
            gain_loop = stack.enter_context(patch.object(self.plugin, "_vibration_gain_loop", new=AsyncMock()))
            await self.plugin._start()
            self.assertIsNotNone(main.Plugin._vibration_gain_task)
            self.assertIsNotNone(main.Plugin._charge_task)
            await asyncio.gather(*(getattr(main.Plugin, name) for name in TASK_NAMES
                                   if getattr(main.Plugin, name) is not None))
            gain_loop.assert_awaited_once()
            self.plugin._charge_loop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
