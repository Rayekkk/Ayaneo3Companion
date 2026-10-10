# SPDX-License-Identifier: BSD-3-Clause
"""Independent charge transactions must remain responsive during TDP recovery."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_logic
import main
from safe_settings import SettingsManager


class IndependentChargeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.store.replace({"charge_bypass": False, "controller": main.DEFAULT_CONTROLLER})
        self.state = {"supported": True, "controller": dict(main.DEFAULT_CONTROLLER),
                      "tdp": dict(main.DEFAULT_TDP), "charge_bypass": False,
                      "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "222"))
        self.stack.enter_context(patch.object(main, "supported_device", return_value=True))
        self.hardware_charge = False
        self.charge_writes = []
        self.stack.enter_context(patch.object(main, "read_charge_bypass", side_effect=lambda: self.hardware_charge))
        self.stack.enter_context(patch.object(main, "write_charge_bypass", side_effect=self.write_charge))
        self.stack.enter_context(patch.object(main, "apply_controller"))
        self.stack.enter_context(patch.object(main, "set_vibration_gain"))
        self.plugin = main.Plugin()

    def write_charge(self, enabled):
        self.hardware_charge = enabled
        self.charge_writes.append(enabled)

    async def held_tdp(self, entered, release):
        def hold():
            with main._tdp_mutation_lock:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test failed to release TDP transaction")
        return await asyncio.to_thread(hold)

    async def test_charge_ack_and_next_rgb_write_do_not_wait_for_tdp_monitor(self):
        entered, release = threading.Event(), threading.Event()
        tasks = [asyncio.create_task(self.held_tdp(entered, release))]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            tasks.append(asyncio.create_task(self.plugin.set_charge_bypass(True)))
            tasks.append(asyncio.create_task(self.plugin.set_controller(
                {**main.DEFAULT_CONTROLLER, "brightness": 40})))
            done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
            acknowledged = len(done)
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(acknowledged, 2, "charge and the next RGB write must complete during TDP recovery")
        self.assertTrue(responses[1]["charge_bypass"])
        self.assertEqual(responses[2]["controller"]["brightness"], 40)
        self.assertTrue(self.store.getSetting("charge_bypass"))

    async def test_charge_drift_recovery_does_not_wait_for_tdp_transaction(self):
        self.store.replace({"charge_bypass": True, "controller": main.DEFAULT_CONTROLLER})
        entered, release = threading.Event(), threading.Event()
        tasks = [asyncio.create_task(self.held_tdp(entered, release))]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            tasks.append(asyncio.create_task(self.plugin._offload(
                self.plugin._restore_saved_control, "charge_bypass", True)))
            done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
            restored = bool(done)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertTrue(restored, "charge recovery is independent of TDP")
        self.assertTrue(self.hardware_charge)
        self.assertEqual(self.charge_writes, [True])

    async def test_noop_game_heartbeat_and_next_rgb_write_do_not_wait_for_tdp_monitor(self):
        entered, release = threading.Event(), threading.Event()
        tasks = [asyncio.create_task(self.held_tdp(entered, release))]
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            tasks.append(asyncio.create_task(self.plugin.set_active_app("222")))
            tasks.append(asyncio.create_task(self.plugin.set_controller(
                {**main.DEFAULT_CONTROLLER, "brightness": 40})))
            done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
            acknowledged = len(done)
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(acknowledged, 2, "a same-game heartbeat must not reserve the queue behind TDP recovery")
        self.assertEqual(responses[2]["controller"]["brightness"], 40)
        self.assertEqual(main.Plugin._active_app, "222")

    async def test_charge_monitor_waits_for_commit_and_uses_new_saved_intent(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_write(enabled):
            self.write_charge(enabled)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test failed to release charge write")
        tasks = []
        with patch.object(main, "write_charge_bypass", side_effect=delayed_write):
            try:
                tasks.append(asyncio.create_task(self.plugin.set_charge_bypass(True)))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin._offload(
                    self.plugin._restore_saved_control, "charge_bypass", True)))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.1)
                raced = bool(done)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertFalse(raced, "the charge monitor must wait for the public transaction's commit")
        self.assertEqual(self.charge_writes, [True])
        self.assertTrue(self.hardware_charge)
        self.assertTrue(self.store.getSetting("charge_bypass"))
