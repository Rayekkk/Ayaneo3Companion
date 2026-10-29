# SPDX-License-Identifier: BSD-3-Clause

import copy
import json
import multiprocessing
import os
import signal
import stat
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import safe_settings


class AtomicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ayaneo3-safe-settings-")
        self.root = Path(self.temp.name) / "settings"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def manager(self):
        return safe_settings.AtomicSettingsManager("module", str(self.root))

    def test_set_is_memory_only_and_commit_writes_complete_snapshot(self):
        manager = self.manager()
        manager.setSetting("first", {"value": 1})
        manager.setSetting("second", {"value": 2})
        self.assertFalse(Path(manager.path).exists())

        manager.commit()
        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8")),
            {"first": {"value": 1}, "second": {"value": 2}},
        )
        self.assertEqual(
            json.loads(Path(manager.backup_path).read_text(encoding="utf-8")),
            {"first": {"value": 1}, "second": {"value": 2}},
        )

    def test_nested_values_defaults_and_data_alias_cannot_leak_mutations(self):
        manager = self.manager()
        value = {"profiles": [1, 2]}
        manager.setSetting("controller", value)
        value["profiles"].append(3)
        manager.getSetting("controller")["profiles"].append(4)
        manager.data["controller"]["profiles"].append(5)
        self.assertEqual(manager.getSetting("controller"), {"profiles": [1, 2]})
        default = {"enabled": []}
        manager.getSetting("missing", default)["enabled"].append(True)
        self.assertEqual(default, {"enabled": []})
        manager.data = {"controller": value}
        value["profiles"].append(6)
        self.assertEqual(manager.getSetting("controller"), {"profiles": [1, 2, 3]})

    def test_nonstandard_or_duplicate_json_recovers_backup_without_silent_data_loss(self):
        for raw in ('{"enabled":true,"enabled":false}', '{"value":NaN}', '{"value":Infinity}'):
            with self.subTest(raw=raw):
                (self.root / "module.json").write_text(raw, encoding="utf-8")
                (self.root / "module.json.bak").write_text('{"kept":true}', encoding="utf-8")
                manager = self.manager()
                self.assertEqual(manager.data, {"kept": True})
                self.assertEqual(manager.recovery_error, "")

    def test_nonfinite_pending_value_is_rejected_and_previous_state_restored(self):
        manager = self.manager()
        manager.replace({"epp": 50})
        manager.setSetting("epp", float("nan"))
        with self.assertRaises(ValueError):
            manager.commit()
        self.assertEqual(manager.data, {"epp": 50})
        self.assertEqual(self.manager().data, {"epp": 50})

    def test_failed_replace_keeps_previous_primary_and_cleans_temporary(self):
        manager = self.manager()
        manager.setSetting("value", "old")
        manager.commit()
        real_replace = safe_settings.os.replace

        def fail_primary(source, destination, *args, **kwargs):
            if os.path.basename(os.fspath(destination)) == "module.json":
                raise OSError("injected replace failure")
            return real_replace(source, destination, *args, **kwargs)

        manager.setSetting("value", "new")
        with patch.object(safe_settings.os, "replace", side_effect=fail_primary):
            with self.assertRaises(OSError):
                manager.commit()

        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8"))["value"],
            "old",
        )
        self.assertEqual(manager.getSetting("value"), "old")
        self.assertFalse(any(path.suffix == ".tmp" for path in self.root.iterdir()))

        # A later successful transaction must not accidentally persist the
        # value rejected above.
        manager.setSetting("other", True)
        manager.commit()
        self.assertEqual(
            json.loads(Path(manager.path).read_text(encoding="utf-8")),
            {"value": "old", "other": True},
        )

    def test_corrupt_primary_is_quarantined_and_restored_from_backup(self):
        manager = self.manager()
        manager.setSetting("value", {"kept": True})
        manager.commit()
        Path(manager.path).write_text("{broken", encoding="utf-8")

        recovered = self.manager()
        self.assertEqual(recovered.getSetting("value"), {"kept": True})
        self.assertEqual(
            json.loads(Path(recovered.path).read_text(encoding="utf-8"))["value"],
            {"kept": True},
        )
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 1)

    @unittest.skipUnless(os.name == "posix", "POSIX directory fsync")
    def test_directory_fsync_failure_after_replace_keeps_committed_value(self):
        manager = self.manager()
        manager.replace({"value": "old"})
        original_fsync = safe_settings.os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("injected directory fsync failure")
            return original_fsync(fd)
        with patch.object(safe_settings.os, "fsync", side_effect=fail_directory):
            manager.replace({"value": "new"})
        self.assertEqual(manager.getSetting("value"), "new")
        self.assertEqual(json.loads(Path(manager.path).read_text(encoding="utf-8")),
                         {"value": "new"})

    def test_existing_primary_gets_a_recovery_backup_on_first_read(self):
        path = self.root / "module.json"
        path.write_text('{"value": 7}', encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.getSetting("value"), 7)
        self.assertEqual(
            json.loads(Path(manager.backup_path).read_text(encoding="utf-8")),
            {"value": 7},
        )

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_existing_primary_is_made_private_on_first_read(self):
        path = self.root / "module.json"
        path.write_text('{"value": 7}', encoding="utf-8")
        os.chmod(path, 0o644)
        self.manager()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_corrupt_file_without_backup_preserves_evidence_across_restarts(self):
        path = self.root / "module.json"
        path.write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.settings, {})
        self.assertEqual(path.read_text(encoding="utf-8"), "[]")
        # A copy of the evidence, made once: the caller's next commit replaces the file itself.
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 1)
        self.assertIn("module.json:", manager.recovery_error)
        # Neither a component reload nor a new process may treat data loss as
        # first install before the caller persists its recovery choice.
        manager.read()
        self.assertTrue(manager.recovery_error)
        self.assertTrue(self.manager().recovery_error)
        self.assertEqual(len(list(self.root.glob("module.json.corrupt-*"))), 1)

    def test_missing_settings_are_distinct_from_failed_recovery(self):
        self.assertEqual(self.manager().recovery_error, "")

    def test_both_corrupt_copies_report_lost_intent_until_a_successful_commit(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.settings, {})
        self.assertIn("module.json:", manager.recovery_error)
        self.assertIn("last-good copy:", manager.recovery_error)  # the old .bak, adopted as the copy
        manager.setSetting("enabled", False)
        with patch.object(safe_settings.dk_fs, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.commit()
        self.assertTrue(manager.recovery_error)
        manager.setSetting("enabled", False)
        manager.commit()
        self.assertEqual(manager.recovery_error, "")
        self.assertEqual(self.manager().settings, {"enabled": False})

    def test_backup_recovery_does_not_report_lost_intent(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text('{"enabled": false}', encoding="utf-8")
        manager = self.manager()
        self.assertFalse(manager.getSetting("enabled"))
        self.assertEqual(manager.recovery_error, "")

    def test_corrupt_backup_without_primary_reports_lost_intent(self):
        (self.root / "module.json.bak").write_text("null", encoding="utf-8")
        manager = self.manager()
        self.assertIn("last-good copy:", manager.recovery_error)
        (self.root / "module.json.last-good").write_text("null", encoding="utf-8")
        self.assertIn("last-good copy:", self.manager().recovery_error)

    def test_excessively_nested_primary_recovers_valid_backup(self):
        # 2000 trips JSON's parser; 600 used to parse successfully and fail in
        # deepcopy instead, bypassing the otherwise valid recovery backup.
        for depth in (600, 2000):
            with self.subTest(depth=depth):
                (self.root / "module.json").write_text(
                    '{"value":' + '[' * depth + '0' + ']' * depth + '}', encoding="utf-8")
                (self.root / "module.json.bak").write_text('{"enabled": false}', encoding="utf-8")
                manager = self.manager()
                self.assertEqual(manager.settings, {"enabled": False})
                self.assertEqual(manager.recovery_error, "")

    def test_an_oversized_file_is_damaged_and_never_read_whole(self):
        (self.root / "module.json").write_bytes(b" " * (safe_settings.MAX_SETTINGS_BYTES + 2))
        (self.root / "module.json.last-good").write_text('{"kept": true}', encoding="utf-8")
        manager = self.manager()
        self.assertEqual((manager.settings, manager.recovery_error), ({"kept": True}, ""))

    def test_settings_of_older_versions_keep_their_recovery_copy(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.bak").write_text('{"enabled": true}', encoding="utf-8")
        self.assertEqual(self.manager().settings, {"enabled": True})
        self.assertFalse((self.root / "module.json.bak").exists())
        self.assertEqual(json.loads((self.root / "module.json.last-good").read_text(encoding="utf-8")),
                         {"enabled": True})

    def test_after_a_downgrade_and_an_update_the_newer_copy_wins(self):
        (self.root / "module.json").write_text("{broken", encoding="utf-8")
        (self.root / "module.json.last-good").write_text('{"enabled": "before the downgrade"}', encoding="utf-8")
        (self.root / "module.json.bak").write_text('{"enabled": "saved by 1.3.0"}', encoding="utf-8")
        os.utime(self.root / "module.json.last-good", (1_000_000_000, 1_000_000_000))
        self.assertEqual(self.manager().settings, {"enabled": "saved by 1.3.0"})
        (self.root / "module.json.bak").write_text('{"enabled": "older"}', encoding="utf-8")
        os.utime(self.root / "module.json.bak", (1_000, 1_000))
        self.manager()
        self.assertTrue((self.root / "module.json.bak").exists(), "an older .bak never replaces the copy")

    def test_an_empty_file_is_a_first_install_not_lost_settings(self):
        (self.root / "module.json").write_text("", encoding="utf-8")
        manager = self.manager()
        self.assertEqual((manager.settings, manager.recovery_error), ({}, ""))

    def test_settings_that_could_not_be_read_back_are_never_written(self):
        manager = self.manager()
        manager.replace({"1": "kept"})
        manager.setSetting(1, "duplicate key once written")
        with self.assertRaises(ValueError):
            manager.commit()
        self.assertEqual(self.manager().settings, {"1": "kept"})

    def test_symlink_target_is_blocked_without_touching_its_destination(self):
        victim = Path(self.temp.name) / "victim"
        victim.write_text("do not change", encoding="utf-8")
        link = self.root / "module.json"
        try:
            link.symlink_to(victim)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        manager = self.manager()
        with self.assertRaises(safe_settings.UnsafeSettingsPath):
            manager.setSetting("attack", True)
        self.assertEqual(victim.read_text(encoding="utf-8"), "do not change")

    @unittest.skipUnless(os.name == "posix", "POSIX permission check")
    def test_committed_files_are_private(self):
        manager = self.manager()
        manager.setSetting("value", 1)
        manager.commit()
        self.assertEqual(stat.S_IMODE(os.stat(manager.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(manager.backup_path).st_mode), 0o600)


# These fixtures exercise real files in fresh child processes. Only the pause
# handshake is injected; each writer is killed before Python cleanup can run.
OLD = {"controller": {"brightness": 0.5, "gyro_enabled": True},
       "game_profiles": {"480": {"sustained": 15, "slow": 20, "fast": 25}}}
PENDING = copy.deepcopy(OLD)
PENDING["controller"] = {"brightness": 0.75, "gyro_enabled": False}


def _storage_child(connection, directory, operation, checkpoint, payload):
    """Pause inside a real write until the parent forcibly kills this process."""
    def pause(reached):
        if reached == checkpoint:
            connection.send({"checkpoint": reached})
            connection.recv()  # Parent kills us; it never acknowledges this.
            raise AssertionError("a crash checkpoint must not resume")

    original_replace = os.replace
    original_fdopen = os.fdopen

    def replace(source, destination, *args, **kwargs):
        leaf = os.path.basename(os.fspath(destination))
        pause("before_replace:" + leaf)
        result = original_replace(source, destination, *args, **kwargs)
        if leaf.startswith("module.json.corrupt-"):
            pause("after_quarantine")
        pause("after_replace:" + leaf)
        return result

    class PartialWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, data):
            middle = len(data) // 2
            self.handle.write(data[:middle])
            self.handle.flush()
            pause("partial_temporary")
            return middle + self.handle.write(data[middle:])

    def fdopen(fd, mode, *args, **kwargs):
        handle = original_fdopen(fd, mode, *args, **kwargs)
        return PartialWriter(handle) if checkpoint == "partial_temporary" and mode == "wb" else handle

    try:
        with patch.object(safe_settings.os, "replace", side_effect=replace), \
             patch.object(safe_settings.os, "fdopen", side_effect=fdopen):
            if operation == "atomic":
                safe_settings.dk_fs.write_json(os.path.join(directory, "module.json"), payload)
            else:
                manager = safe_settings.AtomicSettingsManager("module", directory)
                if operation == "commit":
                    manager.replace(payload)
                connection.send({"settings": manager.settings, "recovery_error": manager.recovery_error})
    except BaseException:
        connection.send({"error": traceback.format_exc()})
    finally:
        connection.close()


def _fifo_reader_child(connection, path, operation):
    """Exercise real FIFO opens in a killable child, including an lstat race."""
    try:
        connection.send("starting")
        name, directory = Path(path).stem, os.path.dirname(path)
        if operation == "harden":
            os.mkfifo(path)
            safe_settings.AtomicSettingsManager(name, directory).data
        else:
            Path(path).write_text('{"safe":true}', encoding="utf-8")
            manager = safe_settings.AtomicSettingsManager(name, directory)
            os.unlink(path)  # swapped for a FIFO after the first read
            os.mkfifo(path)
            manager.read()
        connection.send("unsafe descriptor accepted")
    except safe_settings.UnsafeSettingsPath:
        connection.send("rejected")
    except BaseException:
        connection.send(traceback.format_exc())
    finally:
        connection.close()


@unittest.skipUnless(os.name == "posix", "POSIX FIFO open semantics")
class SettingsFifoTests(unittest.TestCase):
    def assert_fifo_rejected_without_waiting_for_writer(self, operation):
        with tempfile.TemporaryDirectory(prefix="ayaneo3-fifo-") as directory:
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_fifo_reader_child,
                args=(child, str(Path(directory) / "settings.json"), operation))
            process.start()
            child.close()
            try:
                self.assertTrue(parent.poll(10), "child did not start")
                self.assertEqual(parent.recv(), "starting")
                self.assertTrue(parent.poll(3), "settings open blocked waiting for a FIFO writer")
                self.assertEqual(parent.recv(), "rejected")
                process.join(3)
                self.assertEqual(process.exitcode, 0)
            finally:
                if process.is_alive():
                    process.kill()
                    process.join(3)
                parent.close()
                process.close()

    def test_fifo_swapped_after_lstat_does_not_block_settings_read(self):
        self.assert_fifo_rejected_without_waiting_for_writer("read")

    def test_fifo_does_not_block_permission_hardening(self):
        self.assert_fifo_rejected_without_waiting_for_writer("harden")


class SettingsProcessCrashTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ayaneo3-process-crash-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.context = multiprocessing.get_context("spawn")

    def seed(self, payload):
        safe_settings.AtomicSettingsManager("module", str(self.directory)).replace(payload)

    def run_child(self, operation, checkpoint=None, payload=None):
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_storage_child,
                                       args=(child, str(self.directory), operation, checkpoint, payload))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), f"child did not reach {checkpoint or operation} within 10 seconds")
            message = parent.recv()
            self.assertNotIn("error", message, message.get("error"))
            if checkpoint:
                self.assertEqual(message, {"checkpoint": checkpoint})
                self.assertTrue(process.is_alive(), "writer must be alive at the requested checkpoint")
                process.kill()
                process.join(5)
                self.assertFalse(process.is_alive(), "killed writer did not exit")
                self.assertNotEqual(process.exitcode, 0)
                if os.name == "posix":
                    self.assertEqual(process.exitcode, -signal.SIGKILL)
            else:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            return message
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)
            parent.close()
            process.close()

    def assert_reopened(self, expected):
        recovered = self.run_child("read")
        self.assertEqual(recovered["settings"], expected)
        self.assertEqual(recovered["recovery_error"], "")
        self.assertEqual(json.loads((self.directory / "module.json").read_text(encoding="utf-8")), expected)

    def test_kill_with_partial_temporary_json_keeps_the_complete_previous_state(self):
        self.seed(OLD)
        self.run_child("atomic", "partial_temporary", PENDING)
        temporaries = list(self.directory.glob("*.tmp"))
        self.assertEqual(len(temporaries), 1, "abrupt termination must bypass temporary-file cleanup")
        with self.assertRaises(json.JSONDecodeError):
            json.loads(temporaries[0].read_text(encoding="utf-8"))
        self.assert_reopened(OLD)

    def test_kill_at_primary_and_backup_boundaries_keeps_a_complete_transaction(self):
        for checkpoint, primary, backup in (
            ("before_replace:module.json", OLD, OLD),
            ("after_replace:module.json", PENDING, OLD),
            ("before_replace:module.json.last-good", PENDING, OLD),
            ("after_replace:module.json.last-good", PENDING, PENDING),
        ):
            with self.subTest(checkpoint=checkpoint):
                self.seed(OLD)
                self.run_child("commit", checkpoint, PENDING)
                self.assertEqual(json.loads((self.directory / "module.json.last-good").read_text(encoding="utf-8")), backup)
                self.assert_reopened(primary)

    def test_kill_during_backup_recovery_does_not_turn_lost_primary_into_first_install(self):
        for checkpoint in ("after_quarantine", "before_replace:module.json"):
            with self.subTest(checkpoint=checkpoint):
                self.seed(PENDING)
                (self.directory / "module.json").write_text("{broken", encoding="utf-8")
                self.run_child("read", checkpoint)
                # The damaged file stays until the good copy replaces it in one step: never a moment without one.
                self.assertTrue((self.directory / "module.json").exists())
                self.assert_reopened(PENDING)

    def test_kill_before_explicit_repair_keeps_unrecoverable_storage_distinct_from_new_install(self):
        primary = self.directory / "module.json"
        backup = self.directory / "module.json.bak"  # an older version's copy: adopted as .last-good
        primary.write_text("{broken", encoding="utf-8")
        backup.write_text("[]", encoding="utf-8")
        self.run_child("commit", "before_replace:module.json", PENDING)
        recovered = self.run_child("read")
        self.assertEqual(recovered["settings"], {})
        self.assertIn("module.json:", recovered["recovery_error"])
        self.assertIn("last-good copy:", recovered["recovery_error"])
        self.assertEqual(primary.read_text(encoding="utf-8"), "{broken")
        self.assertEqual((self.directory / "module.json.last-good").read_text(encoding="utf-8"), "[]")



if __name__ == "__main__":
    unittest.main()
