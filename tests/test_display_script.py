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
import asyncio
import hashlib
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
import companion_common
import display_backend
import display_backend as display
import display_script_io
from system_process import PYTHON
import main

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


class DisplayDefinitionTests(unittest.TestCase):
    def test_display_script_is_gamma22(self):
        script = display_backend.LUA_SOURCE.read_text()
        self.assertTrue(display_backend.is_our_display_script(display_backend.LUA_SOURCE))
        self.assertIn("gamescope.eotf.gamma22", script)
        self.assertNotIn("gamescope.eotf.pq", script)
        self.assertIn("max_content_light_level = 800", script)
        self.assertIn("max_frame_average_luminance = 400", script)
        self.assertIn("r = { x = 0.6820, y = 0.3150 }", script)
        self.assertIn("g = { x = 0.2400, y = 0.7160 }", script)
        self.assertIn("b = { x = 0.1380, y = 0.0460 }", script)
        self.assertIn("w = { x = 0.3127, y = 0.3290 }", script)

    def test_ayaneo_edid_advertised_maxcll_patch(self):
        base = bytearray(128)
        base[:12] = b"\x00\xff\xff\xff\xff\xff\xff\x00\x07\x21\x13\x01"
        base[126] = 1
        base[127] = (-sum(base[:127])) & 0xff
        cta = bytearray(128)
        cta[0:3] = bytes([2, 3, 15])
        cta[4:8] = bytes([0xe3, 5, 128, 0])
        cta[8:15] = bytes([0xe6, 6, 5, 1, 138, 96, 7])
        cta[127] = (-sum(cta[:127])) & 0xff
        original = bytes(base + cta)
        patched = display_backend.patch_ayaneo_edid(original)
        self.assertIsNotNone(patched)
        self.assertEqual(patched[140], 128)
        self.assertEqual(sum(patched[128:256]) & 0xff, 0)
        self.assertEqual(patched[:140], original[:140])
        self.assertEqual(patched[141:255], original[141:255])
        self.assertAlmostEqual(display_backend.published_edid_nits(patched), 800.0)

    def test_edid_patch_rejects_other_display(self):
        self.assertIsNone(display_backend.patch_ayaneo_edid(bytes(256)))

    def test_edid_loop_checks_the_files_only_when_their_stat_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script, edid = root / "oled.lua", root / "edid.bin"
            script.write_text("ours"); edid.write_bytes(b"x" * 256)
            ticks = []
            async def sleep(_seconds):
                ticks.append(1)
                if len(ticks) == 3:
                    edid.unlink(); edid.write_bytes(b"y" * 256)  # gamescope's next session
                if len(ticks) == 6:
                    raise asyncio.CancelledError
            plugin = main.Plugin()
            async def offload(fn, *args):
                return fn(*args)
            with mock.patch.object(main.display_backend, "LUA_TARGET", script), \
                 mock.patch.object(main.display_backend, "PUBLISHED_EDID", edid), \
                 mock.patch.object(main.display_backend, "is_our_display_script", return_value=True) as ours, \
                 mock.patch.object(main.display_backend, "native_profile_present", return_value=False), \
                 mock.patch.object(main.display_backend, "patch_published_edid", return_value=False) as patch, \
                 mock.patch.object(plugin, "_offload", side_effect=offload), \
                 mock.patch.object(main.asyncio, "sleep", side_effect=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(plugin._edid_loop())
            self.assertEqual(patch.call_count, 2, "once at start, once for the new edid.bin, not every second")
            self.assertEqual(ours.call_count, 2)

    def test_display_definition_upgrades_owned_legacy_file_only(self):
        self.assertTrue({
            "98de33235379f187b9d0eeb1460016b71fbc808c7cecff574774bbae64a130d4",
            "f52b721078df6336855c543da325a908477382cd8f313d395a7eabf1ad9f0b21",
        }.issubset(display_backend.LEGACY_DISPLAY_SCRIPT_SHA256))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.lua"
            target = root / "target.lua"
            current = display_backend.LUA_SOURCE.read_bytes()
            legacy = current.replace(display_backend.DISPLAY_SCRIPT_MARKER, b"", 1)
            self.assertIn(hashlib.sha256(legacy).hexdigest(), display_backend.LEGACY_DISPLAY_SCRIPT_SHA256)
            source.write_bytes(current)
            target.write_bytes(legacy)
            with mock.patch.object(display_backend, "LUA_SOURCE", source), \
                 mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                self.assertTrue(display_backend.display_script_owned(target))
                display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), current)
                target.write_bytes(b"-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), b"-- belongs to somebody else\n")

    def test_display_definition_replacement_requires_confirmation_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.lua"
            target = root / "target.lua"
            backups = root / "backups"
            current = display_backend.LUA_SOURCE.read_bytes()
            foreign = b"-- existing custom display definition\n"
            source.write_bytes(current)
            target.write_bytes(foreign)
            with mock.patch.object(display_backend, "LUA_SOURCE", source), \
                 mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(display_backend, "DISPLAY_SCRIPT_BACKUP_ROOT", backups), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                self.assertTrue(display_backend.display_script_conflict(target))
                with self.assertRaisesRegex(RuntimeError, "requires confirmation"):
                    display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), foreign)
                backup = display_backend.install_display_script(replace_existing=True)
                self.assertIsNotNone(backup)
                self.assertEqual(backup.read_bytes(), foreign)
                self.assertEqual(target.read_bytes(), current)
                self.assertFalse(display_backend.display_script_conflict(target))

    def test_display_definition_removal_is_owned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "display.lua"
            target.write_bytes(display_backend.LUA_SOURCE.read_bytes())
            with mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                display_backend.remove_display_script()
                self.assertFalse(target.exists())
                display_backend.remove_display_script()
                target.write_text("-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    display_backend.remove_display_script()
                self.assertTrue(target.exists())

    def test_display_switch_keeps_installed_definition_when_edid_retry_is_needed(self):
        with mock.patch.object(display_backend, "install_display_script") as install_display, \
             mock.patch.object(display_backend, "is_our_display_script", return_value=True), \
             mock.patch.object(display_backend, "patch_published_edid", side_effect=OSError("busy")), \
             mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock()) as diagnostics, \
             mock.patch.object(main.decky.logger, "warning") as warning:
            result = asyncio.run(main.Plugin().set_screen_fix(True))
        install_display.assert_called_once_with(False)
        warning.assert_called_once()
        self.assertTrue(result["screen_installed"])
        self.assertTrue(main.Plugin._state["screen_installed"])
        diagnostics.assert_not_called()


if __name__ == "__main__":
    unittest.main()
