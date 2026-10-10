# SPDX-License-Identifier: BSD-3-Clause
"""Cancellation must survive a worker failure after shutdown has requested it."""
import asyncio
import unittest

from quality_runtime import complete


class CompletionCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_caller_waits_for_failure_then_keeps_cancellation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        finished = []
        async def worker():
            entered.set()
            await release.wait()
            finished.append(True)
            raise RuntimeError("late worker failure")
        task = asyncio.create_task(complete(worker()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "admitted work must drain before cancellation finishes")
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(finished, [True])

    async def test_failure_completed_at_cancellation_boundary_keeps_cancellation(self):
        worker = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(complete(worker))
        await asyncio.sleep(0)
        task.cancel()
        worker.set_exception(RuntimeError("failure at cancellation boundary"))
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_uncancelled_failure_still_reaches_caller(self):
        async def worker():
            raise RuntimeError("ordinary failure")
        with self.assertRaisesRegex(RuntimeError, "ordinary failure"):
            await complete(worker())


if __name__ == "__main__":
    unittest.main()
