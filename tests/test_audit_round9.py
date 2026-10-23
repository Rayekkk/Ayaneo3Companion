# SPDX-License-Identifier: BSD-3-Clause
"""Charge monitor scheduling and shutdown races; no live hardware writes."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_logic
import main
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
                      "charge_bypass": False, "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.stack.enter_context(patch.object(ec_backend, "both_modules_connected", return_value=False))
        self.stack.enter_context(patch.object(companion_common, "updater", types.SimpleNamespace(close=Mock())))
        for name in ("_restore_task", "_edid_task", "_charge_task", "_module_task",
                     "_tm_guard_task", "_vibration_gain_task", "_audio_task"):
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        self.plugin = main.Plugin()

    async def asyncTearDown(self):
        await asyncio.wait_for(self.plugin._unload(), 3)

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
