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
import power_backend
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

    async def test_fan_auto_remains_responsive_during_settings_disk_io(self):
        with patch.object(main.fan_controller, "set", return_value={
                "supported": True, "mode": "auto", "percent": None, "rpm": 3000}):
            result = await self.assert_loop_responsive(lambda: self.plugin.set_fan("auto"), True)
        self.assertEqual(result["mode"], "auto")

    async def test_game_profile_waits_in_worker_without_stopping_loop(self):
        self.store.replace({"game_profiles": {"111": {**power_backend.PRESETS["Max"], "preset": "Max"}}})
        result = await self.assert_loop_responsive(lambda: self.plugin.get_game_profile("111"), False)
        self.assertTrue(result["exists"])
        self.assertEqual(result["profile"], power_backend.PRESETS["Max"])

    async def test_state_refresh_does_not_block_loop_when_scheduling_diagnostic(self):
        result = await self.assert_loop_responsive(self.plugin.get_state, False)
        self.assertEqual(result["active_app_id"], "111")

    async def test_recovery_rejection_still_prevents_fan_write(self):
        self.store._recovery_error = "No usable settings copy remains"
        with patch.object(main.fan_controller, "set") as fan:
            with self.assertRaisesRegex(RuntimeError, "Settings need recovery"):
                await self.plugin.set_fan("auto")
        fan.assert_not_called()

    async def assert_ownership_failure(self, results, expected_calls):
        with patch.object(subprocess, "run", side_effect=results) as command, \
             patch.object(power_backend, "_ensure_ryzenadj") as ensure, \
             patch.object(power_backend, "write_cpu_boost") as boost:
            with self.assertRaises(power_backend.TdpUnavailableError):
                await self.plugin.set_tdp(power_backend.PRESETS["Max"], "Max", "111")
        self.assertEqual(command.call_count, expected_calls, "a pre-write probe failure must not attempt TDP rollback")
        ensure.assert_not_called()
        boost.assert_not_called()
        self.assertEqual(self.state["tdp_backend"], "Unavailable")
        self.assertFalse(self.state["tdp_supported"])
        self.assertEqual(self.state["tdp"], power_backend.DEFAULT_TDP)
        self.assertIsNone(self.store.getSetting("tdp"))

    async def test_bus_probe_timeout_does_not_repeat_in_hardware_rollback(self):
        await self.assert_ownership_failure(
            subprocess.TimeoutExpired(["busctl"], 5), 1)

    async def test_service_probe_timeout_does_not_repeat_in_hardware_rollback(self):
        def command(arguments, **kwargs):
            if arguments[0] == "busctl":
                return types.SimpleNamespace(returncode=1)
            raise subprocess.TimeoutExpired(arguments, 5)
        await self.assert_ownership_failure(command, 2)

    async def test_missing_ownership_probe_tool_immediately_invalidates_tdp(self):
        await self.assert_ownership_failure(FileNotFoundError("busctl unavailable"), 1)

    async def test_ownership_failure_after_download_still_precedes_device_write(self):
        probes = 0
        def command(arguments, **kwargs):
            nonlocal probes
            if arguments[0] == "busctl":
                probes += 1
                if probes == 2:
                    raise subprocess.TimeoutExpired(arguments, 5)
                return types.SimpleNamespace(returncode=1)
            self.assertEqual(arguments[0], "systemctl")
            return types.SimpleNamespace(returncode=0, stdout="inactive\n")
        with patch.object(subprocess, "run", side_effect=command) as run, \
             patch.object(power_backend, "_ensure_ryzenadj") as ensure, \
             patch.object(power_backend, "write_cpu_boost") as boost:
            with self.assertRaises(power_backend.TdpUnavailableError):
                await self.plugin.set_tdp(power_backend.PRESETS["Max"], "Max", "111")
        self.assertEqual(run.call_count, 3)
        ensure.assert_called_once()
        boost.assert_not_called()
        self.assertFalse(self.state["tdp_supported"])

    async def test_writer_timeout_after_admission_still_attempts_tdp_rollback(self):
        writes = []
        def command(arguments, **kwargs):
            if arguments[0] == "busctl":
                return types.SimpleNamespace(returncode=1)
            if arguments[0] == "systemctl":
                return types.SimpleNamespace(returncode=0, stdout="inactive\n")
            self.assertEqual(arguments[0], str(power_backend.RYZENADJ))
            writes.append(arguments)
            if len(writes) == 1:
                raise subprocess.TimeoutExpired(arguments, 10)
            return types.SimpleNamespace(returncode=0)
        with patch.object(subprocess, "run", side_effect=command), \
             patch.object(power_backend, "_ensure_ryzenadj"), \
             patch.object(power_backend, "write_cpu_boost"):
            with self.assertRaisesRegex(RuntimeError, "previous hardware settings restored"):
                await self.plugin.set_tdp(power_backend.PRESETS["Max"], "Max", "111")
        self.assertEqual(len(writes), 2)
        self.assertIn("--stapm-limit=15000", writes[1])
        self.assertEqual(self.state["tdp"], power_backend.DEFAULT_TDP)
        self.assertIsNone(self.store.getSetting("tdp"))


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
