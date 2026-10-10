# SPDX-License-Identifier: BSD-3-Clause
"""Slow service diagnostics and controller edits across panel lifetimes."""
import asyncio
import threading
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_responsiveness
import main


class AuditRound8Tests(unittest.IsolatedAsyncioTestCase):
    setUp = test_responsiveness.ResponseTests.setUp

    async def asyncTearDown(self):
        task = getattr(self.plugin, "_tdp_diagnostic_task", None)
        if task is not None:
            await asyncio.wait_for(task, 3)

    async def test_partial_controller_write_preserves_other_confirmed_fields(self):
        previous = {**self.controller, "color": "00ff00", "ff_gain": 30}
        self.state["controller"] = previous
        response = await self.plugin.set_controller({"brightness": 40})
        expected = {**previous, "brightness": 40}
        self.assertEqual(response["controller"], expected)
        self.assertEqual(self.store.getSetting("controller"), expected)
        main.apply_controller.assert_called_once_with(expected)
        main.set_vibration_gain.assert_not_called()

    async def test_partial_feedback_and_gain_commit_preserves_rgb(self):
        previous = {**self.controller, "color": "00ff00", "brightness": 40}
        self.state["controller"] = previous
        response = await self.plugin.set_controller_with_vibration_feedback(
            {"vibration": "low", "ff_gain": 20})
        expected = {**previous, "vibration": "low", "ff_gain": 20}
        self.assertEqual(response["controller"], expected)
        main.apply_controller.assert_called_once_with(expected, True, previous["vibration"], False)
        main.set_vibration_gain.assert_called_once_with(20)

    async def test_queued_patch_merges_after_earlier_write_commits(self):
        entered, release = threading.Event(), threading.Event()
        def apply(value):
            if value["color"] == "00ff00" and value["brightness"] != 40:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release controller write")
        tasks = []
        with patch.object(main, "apply_controller", side_effect=apply):
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
        self.assertEqual(main.apply_controller.call_args.args[0], previous)
        self.assertEqual([call.args[0] for call in main.set_vibration_gain.call_args_list], [20, 30])

    async def test_status_returns_while_service_probe_runs_and_coalesces_polling(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "ownership"}
        tasks = []
        with patch.object(main, "tdp_capabilities", side_effect=probe) as capability:
            try:
                tasks.append(asyncio.create_task(self.plugin.get_state()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.extend(asyncio.create_task(self.plugin.get_state()) for _ in range(4))
                done, _ = await asyncio.wait(tasks, timeout=0.25)
                finished = len(done)
                probes = capability.call_count
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
                diagnostic = getattr(self.plugin, "_tdp_diagnostic_task", None)
                if diagnostic is not None:
                    await diagnostic
            latest = await self.plugin.get_state()
        self.assertEqual(finished, 5, "independent status fields must not wait for PowerStation")
        self.assertEqual(probes, 1, "visible-panel polling must share one pending service probe")
        self.assertTrue(all(response["controller_supported"] for response in responses))
        self.assertTrue(all(response["cpu_boost_supported"] for response in responses))
        self.assertEqual(latest["tdp_backend"], "PowerStation")
        self.assertFalse(latest["tdp_supported"])

    async def test_slow_old_probe_cannot_replace_successful_tdp_write(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "stale ownership"}
        tasks = []
        with patch.object(main, "tdp_capabilities", side_effect=probe), patch.object(main, "apply_tdp"):
            try:
                tasks.append(asyncio.create_task(self.plugin.get_state()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.plugin.set_tdp(main.PRESETS["Max"], "Max", "111")
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 3)
                diagnostic = getattr(self.plugin, "_tdp_diagnostic_task", None)
                if diagnostic is not None:
                    await diagnostic
        self.assertTrue(self.state["tdp_supported"], "an observation started before the write is obsolete even if cached values stayed equal")
        self.assertEqual(self.state["tdp_backend"], "RyzenAdj")
        self.assertEqual(self.state["tdp_error"], "")

    async def test_expired_status_observation_refreshes_changed_ownership(self):
        observations = [
            {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "ownership"},
            {"tdp_backend": "RyzenAdj", "tdp_supported": True, "tdp_error": ""},
        ]
        with patch.object(main, "tdp_capabilities", side_effect=observations) as probe:
            await self.plugin.get_state()
            await self.plugin._tdp_diagnostic_task
            cached = await self.plugin.get_state()
            self.assertFalse(cached["tdp_supported"])
            self.assertEqual(probe.call_count, 1, "polls within the diagnostic interval reuse its confirmed result")
            self.plugin._tdp_diagnostic_after = 0.0
            await self.plugin.get_state()
            await self.plugin._tdp_diagnostic_task
            latest = await self.plugin.get_state()
        self.assertTrue(latest["tdp_supported"])
        self.assertEqual(latest["tdp_backend"], "RyzenAdj")
        self.assertEqual(probe.call_count, 2)

    async def test_probe_completing_during_write_cannot_replace_capabilities(self):
        probe_entered, probe_release = threading.Event(), threading.Event()
        write_entered, write_release = threading.Event(), threading.Event()
        def probe():
            probe_entered.set()
            if not probe_release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "old ownership"}
        def apply(value):
            write_entered.set()
            if not write_release.wait(3):
                raise TimeoutError("test did not release TDP write")
        with patch.object(main, "tdp_capabilities", side_effect=probe), patch.object(main, "apply_tdp", side_effect=apply):
            writing = None
            try:
                await self.plugin.get_state()
                self.assertTrue(await asyncio.to_thread(probe_entered.wait, 2))
                writing = asyncio.create_task(self.plugin.set_tdp(main.PRESETS["Max"], "Max", "111"))
                self.assertTrue(await asyncio.to_thread(write_entered.wait, 2))
                probe_release.set()
                await self.plugin._tdp_diagnostic_task
                self.assertTrue(self.state["tdp_supported"], "an old observation is invalid as soon as the write starts")
                await self.plugin.get_state()
                self.assertTrue(self.state["tdp_supported"])
            finally:
                probe_release.set()
                write_release.set()
                if writing is not None:
                    await asyncio.wait_for(writing, 3)
        self.assertTrue(self.state["tdp_supported"])
        self.assertEqual(main.Plugin._tdp_writes, 0)

    async def test_observation_cannot_publish_into_another_backend_lifetime(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "old lifetime"}
        with patch.object(main, "tdp_capabilities", side_effect=probe):
            try:
                await self.plugin.get_state()
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                newer = dict(self.state)
                main.Plugin._state = newer
            finally:
                release.set()
                await self.plugin._tdp_diagnostic_task
        self.assertTrue(newer["tdp_supported"])
        self.assertEqual(newer["tdp_backend"], "RyzenAdj")

    async def test_unload_drains_pending_probe_without_publishing_late_result(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "late result"}
        with patch.object(main, "tdp_capabilities", side_effect=probe), \
             patch.object(main.fan_controller, "close"), \
             patch.object(main, "supported_device", return_value=False):
            try:
                await self.plugin.get_state()
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                unloading = asyncio.create_task(self.plugin._unload())
                done, _ = await asyncio.wait([unloading], timeout=0.05)
                drained_early = bool(done)
            finally:
                release.set()
                await asyncio.wait_for(unloading, 3)
        self.assertFalse(drained_early, "shutdown must wait for the thread that is still using its resources")
        self.assertFalse(self.plugin._workers)
        self.assertIsNone(self.plugin._tdp_diagnostic_task)
        self.assertTrue(self.state["tdp_supported"], "shutdown observations must not publish")

    async def test_initial_controls_do_not_wait_for_slow_ownership_diagnostic(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release diagnostic")
            return {"tdp_backend": "PowerStation", "tdp_supported": False, "tdp_error": "ownership"}
        names = ("_restore_task", "_edid_task", "_ac_task", "_charge_task", "_module_task",
                 "_tm_guard_task", "_vibration_gain_task", "_audio_task", "_fan_task")
        for name in names:
            self.stack.enter_context(patch.object(main.Plugin, name, None))
        for name in ("_restore_hardware", "_edid_loop", "_ac_loop", "_charge_loop", "_module_loop",
                     "_tm_guard_loop", "_vibration_gain_loop", "_restore_audio"):
            self.stack.enter_context(patch.object(main.Plugin, name, new=AsyncMock()))
        for name, value in (("_dmi", "AYANEO 3 isolated fixture"), ("button_map_owned", False),
                            ("_display_script_owned", False), ("ensure_charge_control", None),
                            ("ensure_charge_bypass_control", None), ("audio_fix_installed", False),
                            ("audio_fix_ready", False), ("audio_fix_supported", False)):
            self.stack.enter_context(patch.object(main, name, return_value=value))
        self.stack.enter_context(patch.object(main.fan_controller, "recover_abandoned"))
        self.stack.enter_context(patch.object(main.fan_controller, "close"))
        self.stack.enter_context(patch.object(main, "updater", types.SimpleNamespace(
            reset=Mock(), close=Mock(), ssl_context=Mock(), plugin_version=Mock(return_value="1.2.0"))))
        with patch.object(main, "tdp_capabilities", side_effect=probe):
            try:
                starting = asyncio.create_task(self.plugin._main())
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                done, _ = await asyncio.wait([starting], timeout=0.25)
                started_early = bool(done)
                initial = main.Plugin._copy_state()
            finally:
                release.set()
                await asyncio.wait_for(starting, 3)
                with patch.object(main, "supported_device", return_value=False):
                    await self.plugin._unload()
        self.assertTrue(started_early, "initial CPU and controller controls must not wait for PowerStation")
        self.assertFalse(initial["initializing"])
        self.assertTrue(initial["cpu_boost_supported"])
        self.assertFalse(initial["tdp_supported"], "TDP stays unavailable until ownership has been observed")

    async def test_unsupported_device_never_schedules_ownership_probe(self):
        self.state["supported"] = False
        with patch.object(main, "tdp_capabilities") as probe:
            response = await self.plugin.get_state()
        self.assertFalse(response["tdp_supported"])
        probe.assert_not_called()
        self.assertIsNone(self.plugin._tdp_diagnostic_task)
