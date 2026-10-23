# SPDX-License-Identifier: BSD-3-Clause
"""Startup availability while ALSA discovery is slow; no real device access."""
import asyncio
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_responsiveness
from _harness import owner
import main
import audio_backend
import companion_common


class AudioStartupTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_responsiveness.ResponseTests.setUp

    def prepare_startup(self):
        for name in ("_restore_task", "_edid_task", "_charge_task",
                     "_module_task", "_tm_guard_task", "_vibration_gain_task",
                     "_audio_task"):
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        for name in ("_restore_hardware", "_edid_loop", "_charge_loop",
                     "_module_loop", "_tm_guard_loop", "_vibration_gain_loop", "_restore_audio"):
            self.stack.enter_context(patch.object(self.plugin, name, new=AsyncMock()))
        for name, value in (("dmi", "AYANEO 3 isolated fixture"), ("button_map_owned", False),
                            ("display_script_owned", False), ("ensure_controller_control", None),
                            ("ensure_charge_bypass_control", None), ("audio_fix_installed", True),
                            ("audio_fix_ready", True), ("audio_fix_supported", True)):
            self.stack.enter_context(patch.object(owner(name), name, return_value=value))
        self.stack.enter_context(patch.object(companion_common, "updater", types.SimpleNamespace(
            reset=Mock(), close=Mock(), ssl_context=Mock(), plugin_version=Mock(return_value="1.2.0"))))

    async def assert_diagnostics_deferred(self, name, enabled):
        self.prepare_startup()
        self.store.replace({"audio_fix_enabled": enabled})
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release ALSA probe")
            return True
        with patch.object(owner(name), name, side_effect=probe):
            starting = asyncio.create_task(self.plugin._main())
            try:
                # The probe stays blocked for up to 3 s, so finishing within 2 s
                # proves startup did not wait for it without racing a busy host.
                done, _ = await asyncio.wait([starting], timeout=2)
                available = bool(done)
                initial = main.Plugin._copy_state()
            finally:
                release.set()
                await asyncio.wait_for(starting, 3)
                with patch.object(companion_common, "supported_device", return_value=False):
                    await self.plugin._unload()
        self.assertTrue(available, "slow ALSA diagnostics must not delay unrelated hardware controls")
        self.assertFalse(initial["initializing"])
        self.assertFalse(initial["audio_calibration_available"], "calibration waits for actual DSP readiness")
        self.assertFalse(entered.is_set(), "startup schedules audio discovery without awaiting it")
        self.plugin._restore_audio.assert_awaited_once()

    async def test_installed_audio_readiness_does_not_delay_startup(self):
        await self.assert_diagnostics_deferred("audio_fix_ready", True)

    async def test_disabled_audio_capability_does_not_delay_startup(self):
        await self.assert_diagnostics_deferred("audio_fix_supported", False)

    async def test_disabled_audio_discovery_never_applies_tuning(self):
        self.state.update(audio_fix_enabled=False, audio_fix_supported=False)
        with patch.object(audio_backend, "audio_fix_supported", return_value=True), \
             patch.object(audio_backend, "audio_fix_installed", return_value=False), \
             patch.object(audio_backend, "apply_audio_fix") as apply, \
             patch.object(audio_backend, "audio_fix_ready") as ready:
            result = await self.plugin._offload(self.plugin._restore_audio_once)
        self.assertTrue(result)
        self.assertTrue(self.state["audio_fix_supported"])
        self.assertFalse(self.state["audio_calibration_available"])
        apply.assert_not_called()
        ready.assert_not_called()

    async def test_disabled_audio_discovery_retries_late_alsa(self):
        self.state.update(audio_fix_enabled=False, audio_fix_supported=False)
        with patch.object(audio_backend, "audio_fix_supported", side_effect=[False, True]), \
             patch.object(audio_backend, "audio_fix_installed", return_value=False), \
             patch.object(audio_backend, "apply_audio_fix") as apply, \
             patch.object(main.asyncio, "sleep", new=AsyncMock()):
            await self.plugin._restore_audio()
        self.assertTrue(self.state["audio_fix_supported"])
        apply.assert_not_called()

    async def test_failed_tuning_keeps_supported_controls_available(self):
        self.state.update(audio_fix_enabled=True, audio_fix_supported=False)
        with patch.object(audio_backend, "audio_fix_supported", return_value=True), \
             patch.object(audio_backend, "audio_fix_ready", return_value=False), \
             patch.object(audio_backend, "apply_audio_fix", side_effect=RuntimeError("audio is playing")):
            with self.assertRaisesRegex(RuntimeError, "audio is playing"):
                await self.plugin._offload(self.plugin._restore_audio_once)
        self.assertTrue(self.state["audio_fix_supported"], "a user must still be able to disable retrying tuning")

    async def test_disabled_audio_probe_cannot_publish_after_shutdown(self):
        self.state.update(audio_fix_enabled=False, audio_fix_supported=False)
        def probe():
            self.plugin._closing = True
            return True
        with patch.object(audio_backend, "audio_fix_supported", side_effect=probe), \
             patch.object(audio_backend, "audio_fix_installed", return_value=False):
            await self.plugin._offload(self.plugin._restore_audio_once)
        self.assertFalse(self.state["audio_fix_supported"])

    async def test_audio_probe_cannot_publish_into_another_lifetime(self):
        self.state.update(audio_fix_enabled=False, audio_fix_supported=False)
        newer = dict(self.state)
        def probe():
            main.Plugin._state = newer
            return True
        with patch.object(audio_backend, "audio_fix_supported", side_effect=probe), \
             patch.object(audio_backend, "audio_fix_installed", return_value=False):
            await self.plugin._offload(self.plugin._restore_audio_once)
        self.assertFalse(newer["audio_fix_supported"])

    async def test_shutdown_after_readiness_probe_never_starts_new_dsp_write(self):
        self.state.update(audio_fix_enabled=True, audio_fix_supported=False)
        def probe():
            self.plugin._closing = True
            return False
        with patch.object(audio_backend, "audio_fix_ready", side_effect=probe), \
             patch.object(audio_backend, "audio_fix_supported", return_value=True), \
             patch.object(audio_backend, "apply_audio_fix") as apply:
            await self.plugin._offload(self.plugin._restore_audio_once)
        apply.assert_not_called()

    async def test_unload_finishes_when_cancelled_audio_worker_fails_late(self):
        entered, release = threading.Event(), threading.Event()
        cleanup = threading.Event()
        def failing_worker():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release audio worker")
            if cleanup.is_set():
                return True
            raise RuntimeError("late ALSA failure")
        sleep = asyncio.sleep
        async def quick_retry(delay):
            await sleep(0.001)
        for name in ("_restore_task", "_edid_task", "_charge_task", "_module_task",
                     "_tm_guard_task", "_vibration_gain_task", "_audio_task"):
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        with patch.object(main.asyncio, "sleep", side_effect=quick_retry), \
             patch.object(self.plugin, "_restore_audio_once", side_effect=failing_worker) as restore, \
             patch.object(companion_common, "supported_device", return_value=False), \
             patch.object(companion_common, "updater", types.SimpleNamespace(close=Mock())):
            monitor = asyncio.create_task(self.plugin._restore_audio())
            main.Plugin._audio_task = monitor
            unloading = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                unloading = asyncio.create_task(self.plugin._unload())
                await sleep(0.01)
                self.assertFalse(unloading.done(), "shutdown must wait for admitted audio work")
                release.set()
                done, _ = await asyncio.wait([unloading], timeout=0.2)
                completed = bool(done)
                attempts = restore.call_count
            finally:
                cleanup.set()
                release.set()
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                if unloading is not None:
                    await asyncio.wait_for(unloading, 3)
        self.assertTrue(completed, "worker failure cannot swallow shutdown cancellation and restart a monitor")
        self.assertEqual(attempts, 1)
        self.assertIsNone(main.Plugin._audio_task)
        self.assertFalse(self.plugin._workers)


if __name__ == "__main__":
    unittest.main()
