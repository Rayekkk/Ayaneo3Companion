# SPDX-License-Identifier: BSD-3-Clause
"""Exercise real directory swaps and map recovery without sudo or systemd."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import bootstrap_installer as installer
from bootstrap_installer import ARCHIVE_ROOT, REQUIRED


class Services:
    def __init__(self, target, failure=None, active=True):
        self.target, self.failure = target, failure
        self.calls, self.started = [], []
        self.active = {installer.LOADER: active, installer.INPUT: active}
        self.pids = {installer.LOADER: 101 if active else 0, installer.INPUT: 102 if active else 0}
        self.restarts = {installer.LOADER: 0, installer.INPUT: 0}
        self.status_reads = []
        self.status_hook = None

    def is_active(self, name):
        return self.active[name]

    def status(self, name):
        result = {"active": "active" if self.active[name] else "inactive",
                  "sub": "running" if self.active[name] else "dead",
                  "pid": self.pids[name], "restarts": self.restarts[name], "result": "success"}
        self.status_reads.append(name)
        return self.status_hook(name, result) if self.status_hook else result

    def run(self, action, name):
        self.calls.append((action, name))
        self.active[name] = action != "stop"
        self.pids[name] = self.pids[name] + 100 if self.active[name] else 0
        if action == "start" and name == installer.LOADER:
            self.started.append((self.target / "main.py").read_bytes())
        if self.failure:
            self.failure(action, name)


class BootstrapInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aya3-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.plugins = self.base / "home/gamer/homebrew/plugins"
        self.plugins.mkdir(parents=True)
        self.target = self.plugins / ARCHIVE_ROOT
        self.target.mkdir()
        (self.target / "main.py").write_bytes(b"old backend")
        (self.target / "dist").mkdir()
        (self.target / "dist/index.js").write_bytes(b"old frontend")
        (self.target / "obsolete.py").write_bytes(b"old file")
        self.input_root = self.base / "etc/inputplumber"
        self.map = self.input_root / installer.MAP
        self.map.parent.mkdir(parents=True)
        self.old_map = b"# Managed by AYANEO 3 Companion\nold map\n"
        self.new_map = b"# Managed by AYANEO 3 Companion\nnew map\n"
        self.map.write_bytes(self.old_map)
        self.legacy = self.input_root / installer.LEGACY_MAPS[0]
        self.legacy.parent.mkdir()
        self.legacy.write_bytes(self.old_map + b"legacy")
        files = {name: b"new runtime" for name in REQUIRED}
        files.update({"plugin.json": json.dumps({"name": "AYANEO 3 Companion", "version": "1.2.0",
                                                 "flags": ["root"]}),
                      "package.json": json.dumps({"name": "ayaneo3companion", "version": "1.2.0"}),
                      "main.py": b"new backend", "dist/index.js": b"new frontend",
                      "assets/ayaneo3-companion.yaml": self.new_map})
        self.archive = self.base / "plugin.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data in files.items():
                archive.writestr(f"{ARCHIVE_ROOT}/{name}", data)
        self.services = Services(self.target)
        for name, value in (("SERVICE_TIMEOUT_SECONDS", .15), ("SERVICE_STABLE_SECONDS", .02),
                            ("SERVICE_POLL_SECONDS", .005)):
            context = patch.object(installer, name, value)
            context.start()
            self.addCleanup(context.stop)
        self.before = self.snapshot()

    def snapshot(self):
        return {str(path.relative_to(self.base)): path.read_bytes()
                for root in (self.plugins, self.input_root) for path in root.rglob("*") if path.is_file()}

    def install(self):
        return installer.install_archive(self.archive, self.plugins, self.input_root, self.services)

    def assert_restored(self):
        self.assertEqual(self.snapshot(), self.before)
        self.assertTrue(self.services.active[installer.LOADER])
        self.assertTrue(self.services.active[installer.INPUT])

    def fail_once(self, wanted):
        failed = False
        def fail(action, name):
            nonlocal failed
            if (action, name) == wanted and not failed:
                failed = True
                raise OSError("injected service failure")
        self.services.failure = fail

    def test_service_configuration_finds_non_deck_account_and_quoted_paths(self):
        base = self.plugins.parent.as_posix()
        properties = f'Environment="UNPRIVILEGED_PATH={base}" "PRIVILEGED_PATH={base}"\nWorkingDirectory={base}/services\n'
        self.assertEqual(installer.resolve_plugin_root(properties), self.plugins)
        # A stale PLUGIN_PATH must not override Decky's current privileged base.
        properties += f'Environment="PLUGIN_PATH=/obsolete/plugins" "PRIVILEGED_PATH={base}" UNPRIVILEGED_PATH=/separate-data\n'
        self.assertEqual(installer.resolve_plugin_root(properties), self.plugins)

    def test_working_directory_fallback_and_invalid_configuration(self):
        self.assertEqual(installer.resolve_plugin_root(
            f"WorkingDirectory={self.plugins.parent.as_posix()}/services"), self.plugins)
        for properties in ("", "Environment=UNPRIVILEGED_PATH=/missing PRIVILEGED_PATH=relative",
                           "Environment=PLUGIN_PATH=relative", "Environment=PLUGIN_PATH=${HOMEBREW_FOLDER}/plugins"):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                installer.resolve_plugin_root(properties)

    def test_separate_privileged_and_unprivileged_paths_are_valid(self):
        data = self.base / "user-data"
        data.mkdir()
        properties = (f'Environment="PRIVILEGED_PATH={self.plugins.parent.as_posix()}" '
                      f'"UNPRIVILEGED_PATH={data.as_posix()}"')
        self.assertEqual(installer.resolve_plugin_root(properties), self.plugins)

    def test_unprivileged_base_precedes_legacy_plugin_path(self):
        properties = (f'Environment="UNPRIVILEGED_PATH={self.plugins.parent.as_posix()}" '
                      'PLUGIN_PATH=/obsolete/plugins')
        self.assertEqual(installer.resolve_plugin_root(properties), self.plugins)

    def test_legacy_path_falls_back_to_parent_base_then_plugins(self):
        for suffix in ("plugins", "plugins/", "old-plugin-directory"):
            properties = f'Environment="PLUGIN_PATH={self.plugins.parent.as_posix()}/{suffix}"'
            self.assertEqual(installer.resolve_plugin_root(properties), self.plugins)

    def test_success_replaces_whole_plugin_and_owned_maps(self):
        self.assertEqual(self.install(), "1.2.0")
        self.assertEqual((self.target / "main.py").read_bytes(), b"new backend")
        self.assertEqual((self.target / "dist/index.js").read_bytes(), b"new frontend")
        self.assertFalse((self.target / "obsolete.py").exists())
        self.assertEqual(self.map.read_bytes(), self.new_map)
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.services.started, [b"new backend"])
        self.assertEqual(list(self.plugins.glob(".ayaneo3-install-*")), [])

    def test_staging_failure_never_stops_or_modifies_existing_deployment(self):
        def partial_extract(_zip, destination):
            stage = Path(destination) / ARCHIVE_ROOT
            stage.mkdir()
            (stage / "main.py").write_bytes(b"partial backend")
            raise OSError("injected extraction failure")
        with patch.object(zipfile.ZipFile, "extractall", partial_extract), self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.calls, [])

    def test_plugin_swap_failure_recovers_complete_previous_directory(self):
        replace = os.replace
        def fail(source, target):
            if Path(source).name == ARCHIVE_ROOT and Path(source).parent.name.startswith(".ayaneo3-install-"):
                raise OSError("injected swap failure")
            return replace(source, target)
        with patch.object(installer.os, "replace", side_effect=fail), self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"old backend"])

    def test_map_failure_after_replacement_restores_maps_and_plugin(self):
        copy = installer._atomic_copy
        failed = False
        def fail(source, target):
            nonlocal failed
            copy(source, target)
            if not failed:
                failed = True
                raise OSError("map published before failure")
        with patch.object(installer, "_atomic_copy", side_effect=fail), self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"old backend"])

    def test_backup_copy_failure_never_stops_or_changes_deployment(self):
        with patch.object(installer.shutil, "copy2", side_effect=OSError("backup copy failed")), \
                self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.calls, [])

    def test_legacy_removal_failure_restores_all_maps(self):
        unlink = Path.unlink
        failed = False
        def fail(path, *args, **kwargs):
            nonlocal failed
            if path == self.legacy and not failed:
                failed = True
                unlink(path, *args, **kwargs)
                raise OSError("legacy removal failed after unlink")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail), self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()

    def test_inputplumber_restart_failure_restores_previous_deployment(self):
        self.fail_once(("restart", installer.INPUT))
        with self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"old backend"])

    def test_loader_start_failure_stops_new_plugin_before_recovery(self):
        self.fail_once(("start", installer.LOADER))
        with self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"new backend", b"old backend"])

    def test_failed_stop_recovers_services_without_changing_files(self):
        self.fail_once(("stop", installer.LOADER))
        with self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assert_restored()

    def test_first_install_failure_removes_new_plugin_and_map(self):
        (self.target / "main.py").unlink()
        (self.target / "dist/index.js").unlink()
        (self.target / "dist").rmdir()
        (self.target / "obsolete.py").unlink()
        self.target.rmdir()
        self.map.unlink()
        self.legacy.unlink()
        self.before = self.snapshot()
        self.services = Services(self.target, active=False)
        self.fail_once(("restart", installer.INPUT))
        with self.assertRaisesRegex(RuntimeError, "restored"):
            self.install()
        self.assertEqual(self.snapshot(), self.before)
        self.assertFalse(self.services.active[installer.LOADER])
        self.assertFalse(self.services.active[installer.INPUT])

    def test_foreign_map_refused_before_any_service_changes(self):
        self.map.write_bytes(b"user mapping")
        with self.assertRaisesRegex(ValueError, "Another aya7"):
            self.install()
        self.assertEqual(self.services.calls, [])
        self.assertEqual(self.map.read_bytes(), b"user mapping")
        self.assertEqual((self.target / "main.py").read_bytes(), b"old backend")

    def test_foreign_legacy_map_is_preserved(self):
        self.legacy.write_bytes(b"foreign legacy override")
        self.install()
        self.assertEqual(self.legacy.read_bytes(), b"foreign legacy override")

    def test_incomplete_zip_rejected_before_staging_or_service_calls(self):
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr(f"{ARCHIVE_ROOT}/plugin.json", '{"version":"1.2.0"}')
        with self.assertRaises(ValueError):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.calls, [])

    def test_failed_rollback_retains_backup_and_does_not_restart_loader(self):
        self.fail_once(("restart", installer.INPUT))
        replace = os.replace
        def fail(source, destination):
            if Path(source) == self.target and Path(destination).name == ARCHIVE_ROOT:
                raise OSError("rollback rename failure")
            return replace(source, destination)
        with patch.object(installer.os, "replace", side_effect=fail), self.assertRaisesRegex(RuntimeError, "recovery files retained"):
            self.install()
        recovery = list(self.plugins.glob(".ayaneo3-install-*"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0] / "previous/main.py").read_bytes(), b"old backend")
        self.assertFalse(self.services.active[installer.LOADER])
        self.assertEqual(self.services.started, [])

    def test_exit_zero_but_inactive_loader_rolls_back_before_deleting_previous(self):
        def invisible_failure(action, name):
            if (action, name) == ("start", installer.LOADER) and self.services.started[-1] == b"new backend":
                self.services.active[name] = False
                self.services.pids[name] = 0
        self.services.failure = invisible_failure
        with self.assertRaisesRegex(RuntimeError, "stably running.*previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"new backend", b"old backend"])

    def test_exit_zero_but_inactive_inputplumber_rolls_back_without_starting_new_loader(self):
        failed = False
        def invisible_failure(action, name):
            nonlocal failed
            if (action, name) == ("restart", installer.INPUT) and not failed:
                failed = True
                self.services.active[name] = False
                self.services.pids[name] = 0
        self.services.failure = invisible_failure
        with self.assertRaisesRegex(RuntimeError, "stably running.*previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"old backend"])

    def test_short_lived_loader_is_not_accepted_as_ready(self):
        reads = 0
        def transient(name, status):
            nonlocal reads
            if name == installer.LOADER and self.services.started[-1:] == [b"new backend"]:
                reads += 1
                if reads > 1:
                    return dict(status, active="failed", sub="failed", pid=0, result="exit-code")
            return status
        self.services.status_hook = transient
        with self.assertRaisesRegex(RuntimeError, "previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertGreater(reads, 1)

    def test_inputplumber_failing_during_loader_start_triggers_rollback(self):
        def input_dies(action, name):
            if (action, name) == ("start", installer.LOADER) and self.services.started[-1] == b"new backend":
                self.services.active[installer.INPUT] = False
                self.services.pids[installer.INPUT] = 0
        self.services.failure = input_dies
        with self.assertRaisesRegex(RuntimeError, "stably running.*previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"new backend", b"old backend"])

    def test_failed_recovery_after_inputplumber_dies_retains_previous_backup(self):
        died = False
        def input_dies_then_cannot_restart(action, name):
            nonlocal died
            if (action, name) == ("start", installer.LOADER):
                died = True
                self.services.active[installer.INPUT] = False
                self.services.pids[installer.INPUT] = 0
            if (action, name) == ("restart", installer.INPUT) and died:
                self.services.active[name] = False
                self.services.pids[name] = 0
                raise OSError("cannot recover InputPlumber")
        self.services.failure = input_dies_then_cannot_restart
        with self.assertRaisesRegex(RuntimeError, "recovery files retained"):
            self.install()
        recovery = list(self.plugins.glob(".ayaneo3-install-*"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((self.target / "main.py").read_bytes(), b"old backend")
        self.assertEqual((recovery[0] / ARCHIVE_ROOT / "main.py").read_bytes(), b"new backend")
        self.assertEqual((recovery[0] / "map-0.bak").read_bytes(), self.old_map)
        self.assertFalse(self.services.active[installer.LOADER])
        self.assertEqual(self.services.started, [b"new backend"])

    def test_flapping_pid_and_restarts_time_out_then_restore_old_deployment(self):
        reads = 0
        def flapping(name, status):
            nonlocal reads
            if name == installer.LOADER and self.services.started[-1:] == [b"new backend"] and status["pid"]:
                reads += 1
                return dict(status, pid=1000 + reads, restarts=reads)
            return status
        self.services.status_hook = flapping
        with self.assertRaisesRegex(RuntimeError, "stably running.*previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertGreater(reads, 3)

    def test_failed_readiness_and_failed_rollback_stop_preserve_live_new_files_and_backup(self):
        new_started = False
        def fail(action, name):
            nonlocal new_started
            if (action, name) == ("start", installer.LOADER):
                new_started = True
                raise TimeoutError("start timed out with live new loader")
            if (action, name) == ("stop", installer.LOADER) and new_started:
                self.services.active[name] = True
                self.services.pids[name] = 1001
                raise OSError("cannot stop live new loader")
        self.services.failure = fail
        with self.assertRaisesRegex(RuntimeError, "recovery files retained"):
            self.install()
        recovery = list(self.plugins.glob(".ayaneo3-install-*"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0] / "previous/main.py").read_bytes(), b"old backend")
        self.assertEqual((self.target / "main.py").read_bytes(), b"new backend")
        self.assertEqual(self.map.read_bytes(), self.new_map)
        self.assertTrue(self.services.active[installer.LOADER])

    def test_unconfirmed_stop_preserves_live_new_files_and_backup(self):
        new_started = False
        def timeout_then_ignore_stop(action, name):
            nonlocal new_started
            if (action, name) == ("start", installer.LOADER):
                new_started = True
                raise TimeoutError("start timed out with live new loader")
            if (action, name) == ("stop", installer.LOADER) and new_started:
                self.services.active[name] = True
                self.services.pids[name] = 1001
        self.services.failure = timeout_then_ignore_stop
        with self.assertRaisesRegex(RuntimeError, "fully stopped.*recovery files retained"):
            self.install()
        recovery = list(self.plugins.glob(".ayaneo3-install-*"))
        self.assertEqual((recovery[0] / "previous/main.py").read_bytes(), b"old backend")
        self.assertEqual((self.target / "main.py").read_bytes(), b"new backend")
        self.assertEqual(self.map.read_bytes(), self.new_map)
        self.assertTrue(self.services.active[installer.LOADER])

    def test_initial_stop_requires_pid_zero_before_swapping_files(self):
        ignored = False
        def fail(action, name):
            nonlocal ignored
            if (action, name) == ("stop", installer.LOADER) and not ignored:
                ignored = True
                # Even is-active=false is insufficient while a process remains.
                self.services.active[name] = False
                self.services.pids[name] = 1001
        self.services.failure = fail
        with self.assertRaisesRegex(RuntimeError, "fully stopped.*previous deployment restored"):
            self.install()
        self.assert_restored()
        self.assertEqual(self.services.started, [b"old backend"])

    def lock_in_other_process(self):
        source = """
import pathlib, sys
import bootstrap_installer as installer
try:
    with installer._deployment_lock(pathlib.Path(sys.argv[1]), timeout=.1):
        print('acquired')
except TimeoutError:
    print('blocked')
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.plugins)],
                                cwd=Path(installer.__file__).parent, capture_output=True,
                                text=True, check=True, timeout=10)
        return result.stdout.strip()

    def test_lock_blocks_other_process_and_releases_without_leaving_file(self):
        with installer._deployment_lock(self.plugins):
            self.assertEqual(self.lock_in_other_process(), "blocked")
        self.assertEqual(self.lock_in_other_process(), "acquired")
        self.assertEqual(self.snapshot(), self.before)

    def test_lock_covers_readback_rollback_and_cleanup(self):
        self.fail_once(("restart", installer.INPUT))
        remove = installer._remove_transaction
        checks = []
        def check_during_cleanup(root, transaction):
            checks.append(self.lock_in_other_process())
            return remove(root, transaction)
        def check_during_readback(name, status):
            if not checks:
                checks.append(self.lock_in_other_process())
            return status
        self.services.status_hook = check_during_readback
        with patch.object(installer, "_remove_transaction", side_effect=check_during_cleanup), \
                self.assertRaisesRegex(RuntimeError, "previous deployment restored"):
            self.install()
        self.assertEqual(checks, ["blocked", "blocked"])
        self.assertEqual(self.lock_in_other_process(), "acquired")
        self.assert_restored()

    def install_in_other_process(self):
        source = """
import json, pathlib, sys
import bootstrap_installer as installer
sys.path.insert(0, str(pathlib.Path(installer.__file__).parent / 'tests'))
from test_bootstrap_installer import Services
root, archive, input_root = map(pathlib.Path, sys.argv[1:])
services = Services(root / installer.ARCHIVE_ROOT)
original_lock = installer._deployment_lock
installer._deployment_lock = lambda root: original_lock(root, timeout=.1)
installer.SERVICE_STABLE_SECONDS = .01
installer.SERVICE_POLL_SECONDS = .005
try:
    installer.install_archive(archive, root, input_root, services)
    result = 'installed'
except TimeoutError:
    result = 'blocked'
print(json.dumps({'result': result, 'calls': services.calls, 'readback': services.status_reads}))
"""
        result = subprocess.run([sys.executable, "-c", source, str(self.plugins), str(self.archive),
                                 str(self.input_root)], cwd=Path(installer.__file__).parent,
                                capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def test_concurrent_installer_cannot_read_or_mutate_before_first_rollback_finishes(self):
        failed = False
        observed = []
        def pause_then_fail(action, name):
            nonlocal failed
            if (action, name) == ("restart", installer.INPUT) and not failed:
                failed = True
                observed.append(self.install_in_other_process())
                self.assertEqual((self.target / "main.py").read_bytes(), b"new backend")
                raise OSError("first install fails while second process is excluded")
        self.services.failure = pause_then_fail
        with self.assertRaisesRegex(RuntimeError, "previous deployment restored"):
            self.install()
        self.assertEqual(observed, [{"result": "blocked", "calls": [], "readback": []}])
        self.assert_restored()
        self.assertEqual(self.install_in_other_process()["result"], "installed")
        self.assertEqual((self.target / "main.py").read_bytes(), b"new backend")


class ServiceReadbackTests(unittest.TestCase):
    def test_pid_change_and_restart_count_reset_stability_window(self):
        class Clock:
            now = 0.0
            def monotonic(self):
                return self.now
            def sleep(self, seconds):
                self.now += seconds
        clock = Clock()
        for changed in ({"pid": 20}, {"restarts": 1}):
            with self.subTest(changed=changed):
                clock.now = 0.0
                status = {"active": "active", "sub": "running", "pid": 10,
                          "restarts": 0, "result": "success"}
                def read(_):
                    return dict(status, **changed) if clock.now >= 1.0 else status
                services = unittest.mock.Mock()
                services.status.side_effect = read
                with patch.object(installer.time, "monotonic", clock.monotonic), \
                        patch.object(installer.time, "sleep", clock.sleep):
                    installer._wait_for_service(services, installer.LOADER, True)
                self.assertGreaterEqual(clock.now, 3.0)
                self.assertLess(clock.now, 3.2)

    def test_system_service_status_parses_readback_without_invoking_systemd(self):
        result = subprocess.CompletedProcess([], 0, stdout=(
            "ActiveState=active\nSubState=running\nMainPID=123\nNRestarts=2\nResult=success\n"))
        with patch.object(installer.subprocess, "run", return_value=result) as run:
            status = installer.SystemServices().status(installer.LOADER)
        self.assertEqual(status, {"active": "active", "sub": "running", "pid": 123,
                                  "restarts": 2, "result": "success"})
        self.assertEqual(run.call_args.args[0][:3], ["systemctl", "show", installer.LOADER])
