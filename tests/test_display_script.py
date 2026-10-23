# SPDX-License-Identifier: BSD-3-Clause
"""The gamescope display definition: where it lives, who writes it, when a restart is due."""
from contextlib import ExitStack
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import test_logic
import companion_common
import display_backend as display
import display_script_io
from system_process import PYTHON

POSIX = unittest.skipUnless(os.name == "posix", "POSIX dir_fd and symlink semantics")


class ScriptPlacementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.target = self.root / "config" / "gamescope" / "scripts" / display.SCRIPT_NAME
        self.legacy = self.root / "etc" / display.SCRIPT_NAME
        self.legacy.parent.mkdir(parents=True)
        self.ours = display.LUA_SOURCE.read_bytes()
        for name, value in (("LUA_TARGET", self.target), ("LEGACY_LUA_TARGET", self.legacy),
                            ("NATIVE_LUA", self.root / "usr" / display.SCRIPT_NAME),
                            ("CONFIG_DIR", self.root / "config"), ("SCRIPT_USER", None),
                            ("_loaded_session", None)):
            self.stack.enter_context(patch.object(display, name, value))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))

    def test_install_moves_the_definition_out_of_etc_into_the_users_config(self):
        self.legacy.write_bytes(self.ours)
        display.install_display_script()
        self.assertEqual(self.target.read_bytes(), self.ours)
        self.assertFalse(self.legacy.exists(), "a SteamOS update would wipe the /etc copy anyway")

    def test_a_foreign_etc_definition_is_left_alone(self):
        self.legacy.write_bytes(b"-- someone else's display definition\n")
        display.install_display_script()
        self.assertTrue(self.legacy.exists())

    def test_an_identical_definition_is_not_rewritten(self):
        display.install_display_script()
        os.utime(self.target, (1000, 1000))
        display.install_display_script()
        self.assertEqual(self.target.stat().st_mtime, 1000, "rewriting would look like a new install")

    def test_remove_takes_both_copies_but_never_a_foreign_one(self):
        display.install_display_script()
        self.legacy.write_bytes(self.ours)
        display.remove_display_script()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.legacy.exists())
        self.target.write_bytes(b"-- foreign\n")
        with self.assertRaisesRegex(RuntimeError, "another AYANEO 3"):
            display.remove_display_script()
        self.assertTrue(self.target.exists())

    def test_a_steamos_profile_takes_over_from_ours(self):
        display.install_display_script()
        display.NATIVE_LUA.parent.mkdir(parents=True)
        display.NATIVE_LUA.write_bytes(b"-- shipped by SteamOS\n")
        self.assertIsNone(display.install_display_script())
        self.assertFalse(self.target.exists())

    def test_writes_run_as_the_user_with_no_supplementary_groups(self):
        with patch.object(display, "SCRIPT_USER", (1000, 1001)), \
             patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            display.write_script(b"lua")
            run.return_value = subprocess.CompletedProcess([], 2)
            display.unlink_script()  # already gone is fine
            run.return_value = subprocess.CompletedProcess([], 1)
            with self.assertRaises(OSError):
                display.unlink_script()
            with self.assertRaises(RuntimeError):
                display.write_script(b"lua")
        argv, kwargs = run.call_args_list[0].args[0], run.call_args_list[0].kwargs
        self.assertEqual(argv, [PYTHON, "-I", str(display.SCRIPT_IO), "write", str(self.root / "config"),
                                display.SCRIPT_NAME])
        self.assertEqual((kwargs["user"], kwargs["group"], kwargs["extra_groups"]), (1000, 1001, []))
        self.assertEqual(kwargs["input"], b"lua")
        self.assertNotIn("LD_LIBRARY_PATH", kwargs["env"])

    def test_user_dir_files_the_user_does_not_own_are_not_read(self):
        display.install_display_script()
        owner = self.target.stat().st_uid
        with patch.object(display, "SCRIPT_USER", (owner + 1, 0)):
            self.assertIsNone(display.read_script(self.target))
            self.assertFalse(display.display_script_owned(self.target))
        with patch.object(display, "SCRIPT_USER", (owner, 0)):
            self.assertEqual(display.read_script(self.target), self.ours)

    def test_restart_follows_what_the_running_session_loaded(self):
        started = 2_000_000.0
        with patch.object(display, "gamescope_started", return_value=started):
            self.assertFalse(display.restart_needed(True), "nothing installed yet: no restart to ask for")
            display.install_display_script()
            os.utime(self.target, (started + 1, started + 1))
            self.assertTrue(display.restart_needed(True), "written after gamescope started (the update race)")
            os.utime(self.target, (started - 5, started - 5))
            self.assertFalse(display.restart_needed(True))
            display.remove_display_script()
            self.assertTrue(display.restart_needed(False), "the session still shows the removed definition")
        with patch.object(display, "gamescope_started", return_value=started + 100):
            self.assertFalse(display.restart_needed(False), "a new session never loaded it")

    def test_migration_remembers_the_etc_copy_the_session_loaded(self):
        started = 2_000_000.0
        self.legacy.write_bytes(self.ours)
        os.utime(self.legacy, (started - 5, started - 5))
        with patch.object(display, "gamescope_started", return_value=started):
            display.remember_loaded()
            display.install_display_script()
            os.utime(self.target, (started + 1, started + 1))
            self.assertFalse(display.restart_needed(True), "the same definition is already on screen")

    def test_unknown_session_never_asks_for_a_restart(self):
        with patch.object(display, "gamescope_started", return_value=None):
            display.install_display_script()
            self.assertFalse(display.restart_needed(True))


@POSIX
class UserScriptIoTests(unittest.TestCase):
    """The helper LegionGo2Companion ships, run as the current user."""
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.base = self.root / "config"
        self.dir = self.base / "gamescope" / "scripts"
        self.script = self.dir / display.SCRIPT_NAME

    def helper(self, *argv, data=b""):
        with patch.object(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data))):
            return display_script_io.main(["display_script_io.py", *argv])

    def test_helper_creates_the_tree_and_writes_atomically_readable(self):
        self.assertEqual(self.helper("write", str(self.base), display.SCRIPT_NAME, data=b"lua"), 0)
        self.assertEqual(self.script.read_bytes(), b"lua")
        self.assertEqual(self.script.stat().st_mode & 0o777, 0o644)
        self.assertEqual(sorted(os.listdir(self.dir)), [display.SCRIPT_NAME])
        self.assertEqual(self.helper("unlink", str(self.base), display.SCRIPT_NAME), 0)
        self.assertEqual(self.helper("unlink", str(self.base), display.SCRIPT_NAME), 2)
        for argv in (("write", "relative", "x"), ("write", str(self.base), "../x"), ("chmod", str(self.base), "x")):
            self.assertEqual(self.helper(*argv), 1)

    def test_helper_refuses_symlinked_dirs_and_non_regular_targets(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.base.mkdir()
        (self.base / "gamescope").symlink_to(elsewhere, target_is_directory=True)
        self.assertEqual(self.helper("write", str(self.base), display.SCRIPT_NAME, data=b"lua"), 1)
        self.assertEqual(list(elsewhere.iterdir()), [])
        (self.base / "gamescope").unlink()
        self.dir.mkdir(parents=True)
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        self.script.symlink_to(victim)
        self.assertEqual(self.helper("write", str(self.base), display.SCRIPT_NAME, data=b"lua"), 1)
        self.assertEqual(victim.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
