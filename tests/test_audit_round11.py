# SPDX-License-Identifier: BSD-3-Clause
"""Disk I/O must not stop the Decky event loop; all device access is mocked."""
import subprocess
import asyncio
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, Mock, patch

import test_responsiveness
from _harness import owner
import main
import ec_backend
import safe_settings


class SettingsResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_responsiveness.ResponseTests.setUp
    asyncTearDown = test_responsiveness.ResponseTests.asyncTearDown

    async def assert_loop_responsive(self, action, finishes_during_commit):
        entered, release = threading.Event(), threading.Event()
        write = safe_settings.atomic_write_json
        def stalled_write(path, payload):
            if path == self.store.path:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release the settings write")
            return write(path, payload)

        tasks = []
        with patch.object(safe_settings, "atomic_write_json", side_effect=stalled_write):
            tasks.append(asyncio.create_task(self.plugin._offload(self.store.commit)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # The old implementation blocks the loop itself. A thread
                # releases the test disk even if asyncio cannot run cleanup.
                watchdog = threading.Timer(0.65, release.set)
                watchdog.start()
                started = time.monotonic()
                operation = asyncio.create_task(action())
                tasks.append(operation)
                async def heartbeat():
                    await asyncio.sleep(0.02)
                    return time.monotonic() - started
                beat = asyncio.create_task(heartbeat())
                tasks.append(beat)
                elapsed = await beat
                done, _ = await asyncio.wait([operation], timeout=0.1)
                finished = operation in done
            finally:
                release.set()
                if "watchdog" in locals():
                    watchdog.cancel()
                    watchdog.join()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertLess(elapsed, 0.3, "settings fsync must not block the event loop heartbeat")
        if finishes_during_commit:
            self.assertTrue(finished, "this independent operation must finish before settings disk I/O")
        return results[1]

    async def test_fan_off_remains_responsive_during_settings_disk_io(self):
        # Fan Control keeps its own store and lock: turning it off never waits for main's settings write.
        off = dict(success=True, settings=dict(enabled=False))
        with patch.object(self.plugin._fan, "set_enabled", return_value=off) as set_enabled:
            result = await self.assert_loop_responsive(lambda: self.plugin.fan_set_enabled(False), True)
        self.assertFalse(result["settings"]["enabled"])
        set_enabled.assert_awaited_once_with(False)


class RawEcPowerTests(unittest.TestCase):
    def setUp(self):
        self.ec = MagicMock()
        self.ec.__enter__.return_value = self.ec
        self.ec.write.return_value = 1
        self.path = Mock()
        self.path.open.return_value = self.ec
        for target, value in (("supported_device", True), ("ensure_charge_control", self.path)):
            fixture = patch.object(owner(target), target, return_value=value)
            fixture.start()
            self.addCleanup(fixture.stop)
        fixture = patch.object(ec_backend, "AYANEO_EC_ROOT", Mock(exists=Mock(return_value=False)))
        fixture.start()
        self.addCleanup(fixture.stop)
        self.on = bytes([ec_backend.EC_CONTROLLER_POWER_ON])
        self.off = bytes([ec_backend.EC_CONTROLLER_POWER_OFF])

    def test_raw_ec_power_write_requires_matching_readback(self):
        self.ec.read.side_effect = [self.off, self.on]
        ec_backend.set_controller_power(True)
        self.assertEqual(self.ec.read.call_count, 2)
        self.ec.write.assert_called_once_with(self.on)

    def test_raw_ec_ignored_power_write_reports_failure_and_restores_previous(self):
        self.ec.read.side_effect = [self.on, self.on, self.on]
        with self.assertRaisesRegex(RuntimeError, "did not retain"):
            ec_backend.set_controller_power(False)
        self.assertEqual([call.args[0] for call in self.ec.write.call_args_list], [self.off, self.on])

    def test_raw_ec_unreadable_previous_power_never_writes(self):
        self.ec.read.return_value = b""
        with self.assertRaisesRegex(RuntimeError, "could not read"):
            ec_backend.set_controller_power(False)
        self.ec.write.assert_not_called()

    def test_raw_ec_failed_power_recovery_is_reported(self):
        self.ec.read.side_effect = [self.on, self.on, self.off]
        with self.assertRaisesRegex(RuntimeError, "recovery failed.*readback mismatch"):
            ec_backend.set_controller_power(False)

    def test_raw_ec_power_rejects_non_boolean_before_write(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            ec_backend.set_controller_power(1)
        self.ec.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
