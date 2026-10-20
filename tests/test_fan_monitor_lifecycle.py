# SPDX-License-Identifier: BSD-3-Clause
"""A late result from an old fan renewal must not orphan a new session."""
import asyncio
from contextlib import ExitStack
from itertools import chain, repeat
import sys
import types
import unittest
from unittest.mock import Mock, patch

if "main" not in sys.modules:
    from _harness import install
    install()
import main
import companion_common


class FanMonitorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_late_renewal(self, outcome, replace_session, resume=False):
        finished, publish, next_tick = (asyncio.Event() for _ in range(3))
        original_sleep = asyncio.sleep
        sleeps = 0
        fan = types.SimpleNamespace(
            set=Mock(return_value={"supported": True, "mode": "manual", "percent": 80, "rpm": 3000}),
            renew=Mock(), close=Mock())
        plugin = main.Plugin()
        operation = fan.close if resume else fan.renew
        offset = Mock(side_effect=chain([0], repeat(main.CONTROLLER_RESUME_THRESHOLD + 1))) if resume else lambda: 0

        async def offload(function, *args):
            if function is operation:
                operation()
                # Model the worker result awaiting delivery to the event loop.
                # Its hardware lock is already released, so a new write can run.
                finished.set()
                await publish.wait()
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
            return function(*args)

        async def sleep(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 1:
                await original_sleep(0)
            else:
                await next_tick.wait()

        with ExitStack() as stack:
            for target, name, value in (
                (main, "fan_controller", fan),
                (main, "settings", types.SimpleNamespace(recovery_error="")),
                (companion_common, "supported_device", lambda: True),
                (main, "_suspend_clock_offset", offset),
                (main.Plugin, "_state", {"supported": True}),
                (main.Plugin, "_fan_task", None),
                (plugin, "_offload", offload),
                (asyncio, "sleep", sleep),
            ):
                stack.enter_context(patch.object(target, name, value))
            try:
                await plugin.set_fan("manual", 60)
                await asyncio.wait_for(finished.wait(), 1)
                first_task = main.Plugin._fan_task
                if replace_session:
                    await plugin.set_fan("manual", 80)
                publish.set()
                for _ in range(8):
                    await original_sleep(0)
                if replace_session:
                    self.assertFalse(main.Plugin._fan_task.done(),
                                     "the old renewal result must not stop the new manual session's monitor")
                    self.assertGreaterEqual(sleeps, 2)
                else:
                    self.assertTrue(first_task.done(), "a finished session must stop its monitor")
                    await plugin.set_fan("manual", 80)
                    self.assertIsNot(main.Plugin._fan_task, first_task)
                    self.assertFalse(main.Plugin._fan_task.done())
            finally:
                publish.set()
                if main.Plugin._fan_task is not None:
                    main.Plugin._fan_task.cancel()
                    await asyncio.gather(main.Plugin._fan_task, return_exceptions=True)

    async def test_expired_renewal_cannot_stop_a_new_manual_monitor(self):
        await self.exercise_late_renewal(False, True)

    async def test_failed_renewal_cannot_stop_a_new_manual_monitor(self):
        await self.exercise_late_renewal(RuntimeError("old session expired"), True)

    async def test_finished_session_still_stops_and_next_write_starts_a_monitor(self):
        await self.exercise_late_renewal(False, False)

    async def test_old_resume_cleanup_cannot_stop_a_new_manual_monitor(self):
        await self.exercise_late_renewal(None, True, resume=True)

    async def test_failed_session_still_stops_and_next_write_starts_a_monitor(self):
        await self.exercise_late_renewal(RuntimeError("renewal failed"), False)

    async def test_resume_cleanup_stops_and_next_write_starts_a_monitor(self):
        await self.exercise_late_renewal(None, False, resume=True)

    async def test_failed_resume_cleanup_stops_and_next_write_starts_a_monitor(self):
        await self.exercise_late_renewal(RuntimeError("resume cleanup failed"), False, resume=True)

    async def test_failed_old_resume_cleanup_keeps_a_new_manual_monitor(self):
        await self.exercise_late_renewal(RuntimeError("old resume cleanup failed"), True, resume=True)
