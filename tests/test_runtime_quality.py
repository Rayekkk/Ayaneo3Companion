# SPDX-License-Identifier: BSD-3-Clause
"""Lifecycle and durable transaction tests with no console/host hardware access."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
if "main" not in sys.modules:
    from _harness import install
    install()
import main
import safe_settings


TASK_NAMES = ("_restore_task", "_edid_task", "_ac_task", "_module_task", "_tm_guard_task", "_audio_task")
BASE_TDP = {"spl": 15, "sppt": 20, "fppt": 25}
NEXT_TDP = {"spl": 20, "sppt": 25, "fppt": 30}


class RuntimeQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ayaneo3-runtime-quality-")
        self.addCleanup(self.temp.cleanup)
        self.previous_state = copy.deepcopy(main.Plugin._state)
        self.previous_app = main.Plugin._active_app
        self.previous_tasks = {name: getattr(main.Plugin, name) for name in TASK_NAMES}
        for name in TASK_NAMES:
            setattr(main.Plugin, name, None)
        self.store = safe_settings.SettingsManager("settings", self.temp.name)
        self.controller = copy.deepcopy(main.DEFAULT_CONTROLLER)
        self.store.replace({"tdp": BASE_TDP, "tdp_preset": "Custom",
                            "controller": self.controller, "cpu_boost": False,
                            "charge_bypass": False, "game_profiles": {}})
        self.hardware = {"tdp": dict(BASE_TDP), "controller": copy.deepcopy(self.controller),
                         "cpu_boost": False, "charge_bypass": False}
        self.writes = []
        self.releases = []
        self.patchers = []

        def patch(name, **kwargs):
            patcher = mock.patch.object(main, name, **kwargs)
            result = patcher.start()
            self.patchers.append(patcher)
            return result

        self.patch = patch
        patch("settings", new=self.store)
        self.device = patch("supported_device", return_value=True)
        patch("both_modules_connected", return_value=True)
        patch("controller_powered", return_value=True)
        patch("set_controller_power", side_effect=AssertionError("unexpected controller power write"))
        patch("read_cpu_boost", side_effect=lambda: self.hardware["cpu_boost"])
        patch("read_charge_bypass", side_effect=lambda: self.hardware["charge_bypass"])
        patch("write_cpu_boost", side_effect=lambda value: self.write("cpu_boost", value))
        patch("write_charge_bypass", side_effect=lambda value: self.write("charge_bypass", value))
        patch("apply_controller", side_effect=lambda value, *a, **kw: self.write("controller", value))
        patch("apply_tdp", side_effect=self.apply_tdp)
        patch("updater", new=types.SimpleNamespace(
            reset=mock.Mock(), close=mock.Mock(), ssl_context=mock.Mock(return_value=None),
            plugin_version=mock.Mock(return_value="1.0.2"),
            check=mock.Mock(return_value={"current_version": "1.0.2", "update_available": False}),
            download_latest=mock.Mock(return_value={"success": False})))
        main.Plugin._state = {
            "supported": True, "initializing": False, "startup_error": "",
            "tdp": dict(BASE_TDP), "tdp_preset": "Custom", "controller": copy.deepcopy(self.controller),
            "cpu_boost": False, "charge_bypass": False,
            "cpu_boost_supported": True, "charge_bypass_supported": True,
            "modules_reconnecting": False, "modules_connected": True,
            "tm_guard_enabled": True, "tm_guard_status": "Monitoring", "tm_guard_recoveries": 0,
        }
        main.Plugin._active_app = "480"
        self.snapshot_patcher = mock.patch.object(
            main.Plugin, "_snapshot", side_effect=lambda: copy.deepcopy(main.Plugin._state))
        self.snapshot_patcher.start()
        self.plugin = main.Plugin()

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        try:
            await asyncio.wait_for(self.plugin._unload(), 5)
        finally:
            self.snapshot_patcher.stop()
            for patcher in reversed(self.patchers):
                patcher.stop()
            main.Plugin._state = self.previous_state
            main.Plugin._active_app = self.previous_app
            for name, task in self.previous_tasks.items():
                setattr(main.Plugin, name, task)

    def write(self, field, value):
        self.writes.append((field, copy.deepcopy(value)))
        self.hardware[field] = copy.deepcopy(value)

    def apply_tdp(self, value):
        self.write("tdp", value)
        # The driver/firmware may reset this independently during a power write.
        self.hardware["cpu_boost"] = True

    def checkpoint(self):
        return (copy.deepcopy(main.Plugin._state), copy.deepcopy(self.hardware), self.store.data,
                Path(self.store.path).read_bytes(), Path(self.store.backup_path).read_bytes())

    def assert_restored(self, before):
        state, hardware, settings, primary, backup = before
        self.assertEqual(main.Plugin._state, state)
        self.assertEqual(self.hardware, hardware)
        self.assertEqual(self.store.data, settings)
        self.assertEqual(Path(self.store.path).read_bytes(), primary)
        self.assertEqual(Path(self.store.backup_path).read_bytes(), backup)
        self.assertEqual(safe_settings.SettingsManager("settings", self.temp.name).data, settings)

    def fail_primary_replace(self):
        original = safe_settings.os.replace
        def replace(source, destination, *args, **kwargs):
            if os.path.basename(os.fspath(destination)) == "settings.json":
                raise OSError("injected full filesystem")
            return original(source, destination, *args, **kwargs)
        return mock.patch.object(safe_settings.os, "replace", side_effect=replace)

    def blocking_boost_write(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def write(value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test never released the isolated worker")
            self.write("cpu_boost", value)
        self.patch("write_cpu_boost", side_effect=write)
        return entered, release

    async def wait_entered(self, entered):
        self.assertTrue(await asyncio.to_thread(entered.wait, 2), "worker did not reach checkpoint")

    async def test_global_tdp_failed_commit_restores_disk_state_and_hardware(self):
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaisesRegex(RuntimeError, "restored"):
            await self.plugin.set_tdp(NEXT_TDP, "Custom", "480")
        self.assert_restored(before)
        self.assertTrue(any(field == "tdp" for field, _ in self.writes))

    async def test_unmanaged_cpu_boost_is_restored_after_tdp_commit_failure(self):
        values = self.store.data
        values.pop("cpu_boost")
        self.store.replace(values)
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.set_tdp(NEXT_TDP, "Custom", "480")
        self.assert_restored(before)
        self.assertFalse(self.hardware["cpu_boost"])
        self.assertNotIn("cpu_boost", self.store.data)

    async def test_game_profile_failed_commit_does_not_create_rejected_profile(self):
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.set_game_profile("480", NEXT_TDP, "Custom", "480")
        self.assert_restored(before)
        self.assertFalse((await self.plugin.get_game_profile("480"))["exists"])

    async def test_active_profile_delete_failed_commit_keeps_active_game_values(self):
        self.store.setSetting("game_profiles", {"480": {**NEXT_TDP, "preset": "Custom"}})
        self.store.commit()
        self.hardware["tdp"] = dict(NEXT_TDP)
        main.Plugin._state["tdp"] = dict(NEXT_TDP)
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.delete_game_profile("480", "480")
        self.assert_restored(before)

    async def test_controller_failed_commit_restores_previous_configuration(self):
        before = self.checkpoint()
        wanted = {**self.controller, "brightness": 25, "color": "00ff00"}
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.set_controller(wanted)
        self.assert_restored(before)
        self.assertEqual([field for field, _ in self.writes], ["controller", "controller"])

    async def test_cpu_boost_failed_commit_restores_actual_kernel_value(self):
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.set_cpu_boost(True)
        self.assert_restored(before)
        self.assertEqual(self.writes, [("cpu_boost", True), ("cpu_boost", False)])

    async def test_charge_bypass_failed_commit_restores_actual_charge_value(self):
        before = self.checkpoint()
        with self.fail_primary_replace(), self.assertRaises(RuntimeError):
            await self.plugin.set_charge_bypass(True)
        self.assert_restored(before)
        self.assertEqual(self.writes, [("charge_bypass", True), ("charge_bypass", False)])

    async def test_partial_controller_apply_failure_is_rolled_back_before_reporting(self):
        before = self.checkpoint()
        wanted = {**self.controller, "brightness": 25}
        attempts = []
        def apply(value):
            self.write("controller", value)
            attempts.append(value)
            if len(attempts) == 1:
                raise OSError("controller disappeared after first packet")
        with mock.patch.object(main, "apply_controller", side_effect=apply), self.assertRaisesRegex(RuntimeError, "restored"):
            await self.plugin.set_controller(wanted)
        self.assert_restored(before)

    async def test_failed_hardware_rollback_is_reported_without_claiming_success(self):
        disk = self.store.data
        with mock.patch.object(main, "apply_controller", side_effect=OSError("disconnected")), \
                self.assertRaisesRegex(RuntimeError, "hardware rollback failed"):
            await self.plugin.set_controller({**self.controller, "brightness": 25})
        self.assertEqual(self.store.data, disk)

    async def test_stale_game_context_cannot_touch_settings_or_hardware(self):
        before = self.checkpoint()
        for operation in (
            lambda: self.plugin.set_tdp(NEXT_TDP, "Custom", "old-game"),
            lambda: self.plugin.set_game_profile("480", NEXT_TDP, "Custom", "old-game"),
            lambda: self.plugin.delete_game_profile("480", "old-game"),
        ):
            with self.assertRaisesRegex(RuntimeError, "no longer active"):
                await operation()
        self.assert_restored(before)
        self.assertEqual(self.writes, [])

    async def test_successful_game_profile_keeps_global_and_restores_it_on_exit(self):
        await self.plugin.set_game_profile("480", NEXT_TDP, "Custom", "480")
        persisted = safe_settings.SettingsManager("settings", self.temp.name).data
        self.assertEqual(persisted["tdp"], BASE_TDP)
        self.assertEqual(persisted["game_profiles"]["480"], {**NEXT_TDP, "preset": "Custom"})
        self.assertEqual(self.hardware["tdp"], NEXT_TDP)
        await self.plugin.set_active_app("")
        self.assertEqual(self.hardware["tdp"], BASE_TDP)
        self.assertFalse(self.hardware["cpu_boost"])
        self.assertEqual(main.Plugin._active_app, "")

    async def test_repeated_caller_cancellation_waits_for_real_worker_to_commit(self):
        entered, release = self.blocking_boost_write()
        caller = asyncio.create_task(self.plugin.set_cpu_boost(True))
        await self.wait_entered(entered)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(caller.done())
        self.assertFalse(self.store.getSetting("cpu_boost"))
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(self.hardware["cpu_boost"])
        self.assertTrue(self.store.getSetting("cpu_boost"))
        self.assertTrue(main.Plugin._state["cpu_boost"])

    async def test_cancelled_unload_and_duplicate_unload_wait_for_the_same_worker(self):
        entered, release = self.blocking_boost_write()
        caller = asyncio.create_task(self.plugin.set_cpu_boost(True))
        await self.wait_entered(entered)
        first = asyncio.create_task(self.plugin._unload())
        await asyncio.sleep(0)
        second = asyncio.create_task(self.plugin._unload())
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertFalse(caller.done())
        release.set()
        await caller
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertTrue(self.hardware["cpu_boost"])
        self.assertTrue(self.store.getSetting("cpu_boost"))
        self.assertFalse(self.plugin._rpc_jobs)
        self.assertFalse(self.plugin._workers)
        self.assertEqual(main.updater.close.call_count, 1)

    async def test_shutdown_rejects_queued_write_but_drains_admitted_transaction(self):
        entered, release = self.blocking_boost_write()
        admitted = asyncio.create_task(self.plugin.set_cpu_boost(True))
        await self.wait_entered(entered)
        queued = asyncio.create_task(self.plugin.set_charge_bypass(True))
        await asyncio.sleep(0)
        unload = asyncio.create_task(self.plugin._unload())
        await asyncio.sleep(0.01)
        self.assertFalse(unload.done())
        release.set()
        await admitted
        with self.assertRaisesRegex(RuntimeError, "shutting down"):
            await queued
        await unload
        self.assertTrue(self.store.getSetting("cpu_boost"))
        self.assertFalse(self.store.getSetting("charge_bypass"))
        self.assertFalse(any(field == "charge_bypass" for field, _ in self.writes))

    async def test_new_calls_after_close_do_not_start_hardware_or_network_workers(self):
        await self.plugin._unload()
        before = self.checkpoint()
        for operation in (self.plugin.get_state, self.plugin.check_for_updates,
                          lambda: self.plugin.set_cpu_boost(True)):
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                await operation()
        self.assert_restored(before)
        main.updater.check.assert_not_called()
        self.assertEqual(self.writes, [])

    async def test_unload_during_startup_drains_worker_and_never_leaves_late_monitors(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        created = []
        def blocking_setup():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test never released startup worker")
        async def start():
            await self.plugin._offload(blocking_setup)
            created.append(True)
            main.Plugin._restore_task = asyncio.create_task(asyncio.sleep(100))
        self.plugin._start = start
        starting = asyncio.create_task(self.plugin._main())
        await self.wait_entered(entered)
        unloading = asyncio.create_task(self.plugin._unload())
        await asyncio.sleep(0.01)
        self.assertFalse(unloading.done())
        self.assertFalse(starting.done())
        release.set()
        await asyncio.wait_for(unloading, 2)
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertEqual(created, [])
        self.assertTrue(all(getattr(main.Plugin, name) is None for name in TASK_NAMES))
        self.assertFalse(self.plugin._workers)

    async def test_corrupt_settings_startup_exposes_error_without_hardware_defaults(self):
        Path(self.store.path).write_text("{broken", encoding="utf-8")
        Path(self.store.backup_path).write_text("[]", encoding="utf-8")
        self.snapshot_patcher.stop()
        self.patch("_dmi", return_value="AYANEO 3 isolated fixture")
        self.patch("button_map_owned", return_value=False)
        self.patch("_display_script_owned", return_value=False)
        self.patch("_display_script_conflict", return_value=False)
        forbidden = ["ensure_charge_control", "ensure_charge_bypass_control", "audio_fix_installed",
                     "audio_fix_ready", "audio_fix_supported", "tdp_backend", "gpu_power_watts",
                     "install_button_fix", "install_display_script", "apply_audio_fix"]
        spies = [self.patch(name, side_effect=AssertionError("hardware queried on lost settings"))
                 for name in forbidden]
        await self.plugin._main()
        state = await self.plugin.get_state()
        self.assertTrue(state["settings_error"])
        self.assertFalse(state["startup_error"])
        self.assertFalse(state["initializing"])
        with self.assertRaisesRegex(RuntimeError, "Settings need recovery"):
            await self.plugin.set_cpu_boost(True)
        self.assertEqual(self.writes, [])
        for spy in spies:
            spy.assert_not_called()
        self.assertTrue(all(getattr(main.Plugin, name) is None for name in TASK_NAMES))
        self.assertEqual(Path(self.store.path).read_text(encoding="utf-8"), "{broken")
        self.assertEqual(Path(self.store.backup_path).read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
