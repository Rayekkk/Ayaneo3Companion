# SPDX-License-Identifier: BSD-3-Clause
"""Slow service diagnostics and controller edits across panel lifetimes."""
import asyncio
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_responsiveness
from _harness import owner
import main
import controller_backend
import companion_common


class PartialControllerWriteTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_responsiveness.ResponseTests.setUp

    async def test_partial_controller_write_preserves_other_confirmed_fields(self):
        previous = {**self.controller, "color": "00ff00", "ff_gain": 30}
        self.state["controller"] = previous
        response = await self.plugin.set_controller({"brightness": 40})
        expected = {**previous, "brightness": 40}
        self.assertEqual(response["controller"], expected)
        self.assertEqual(self.store.getSetting("controller"), expected)
        controller_backend.apply_controller.assert_called_once_with(expected)
        controller_backend.set_vibration_gain.assert_not_called()

    async def test_partial_feedback_and_gain_commit_preserves_rgb(self):
        previous = {**self.controller, "color": "00ff00", "brightness": 40}
        self.state["controller"] = previous
        response = await self.plugin.set_controller_with_vibration_feedback(
            {"vibration": "low", "ff_gain": 20})
        expected = {**previous, "vibration": "low", "ff_gain": 20}
        self.assertEqual(response["controller"], expected)
        controller_backend.apply_controller.assert_called_once_with(expected, True, previous["vibration"], False)
        controller_backend.set_vibration_gain.assert_called_once_with(20)

    async def test_queued_patch_merges_after_earlier_write_commits(self):
        entered, release = threading.Event(), threading.Event()
        def apply(value):
            if value["color"] == "00ff00" and value["brightness"] != 40:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release controller write")
        tasks = []
        with patch.object(controller_backend, "apply_controller", side_effect=apply):
            try:
                tasks.append(asyncio.create_task(self.plugin.set_controller({"color": "00ff00"})))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin.set_controller({"brightness": 40})))
                await asyncio.sleep(0)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(responses[-1]["controller"], {**self.controller, "color": "00ff00", "brightness": 40})

    async def test_failed_partial_commit_restores_entire_previous_controller(self):
        previous = {**self.controller, "color": "00ff00", "ff_gain": 30}
        self.state["controller"] = previous
        self.store.replace({"controller": previous})
        with patch.object(self.store, "commit", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "previous hardware settings restored"):
                await self.plugin.set_controller({"brightness": 40, "ff_gain": 20})
        self.assertEqual(self.state["controller"], previous)
        self.assertEqual(self.store.getSetting("controller"), previous)
        self.assertEqual(controller_backend.apply_controller.call_args.args[0], previous)
        self.assertEqual([call.args[0] for call in controller_backend.set_vibration_gain.call_args_list], [20, 30])

