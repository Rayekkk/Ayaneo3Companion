# SPDX-License-Identifier: BSD-3-Clause
"""Run the shipped Desktop Exec against harmless temporary bootstrap scripts."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "Install AYANEO 3 Companion.desktop"


class DesktopLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aya3-launcher-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.arguments = shlex.split(next(line[5:] for line in
            DESKTOP.read_text(encoding="utf-8").splitlines() if line.startswith("Exec=")))

    def command(self, location):
        self.assertEqual(self.arguments[-1], "%k", "the desktop location must be a standalone argument")
        self.assertEqual(self.arguments[:2], ["python3", "-c"])
        self.assertNotIn("%k", self.arguments[2], "never interpolate the location into executable code")
        return [sys.executable, *self.arguments[1:-1], location]

    def fixture(self, name="installer", exit_code=0):
        directory = self.base / name
        directory.mkdir()
        launcher = directory / DESKTOP.name
        launcher.write_bytes(DESKTOP.read_bytes())
        # Only this harmless script runs; no actual bootstrap, sudo or services.
        (directory / "bootstrap.sh").write_text(
            f"printf '%s\\n' ran > ran.txt\nexit {exit_code}\n", encoding="utf-8")
        return launcher

    def run_launcher(self, location):
        environment = os.environ.copy()
        # A Windows runner's first bash.exe may be the WSL launcher, which
        # cannot consume Windows paths or run without an installed distro.
        # Git Bash runs the harmless fixture with native Windows arguments.
        bash = (str(Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                    / "Git" / "bin" / "bash.exe")
                if os.name == "nt" else shutil.which("bash"))
        if not bash or not Path(bash).is_file():
            self.skipTest("Bash is unavailable")
        environment["PATH"] = str(Path(bash).parent) + os.pathsep + environment.get("PATH", "")
        return subprocess.run(self.command(location), input="\n", capture_output=True,
                              text=True, timeout=10, cwd=self.base, env=environment)

    @unittest.skipUnless(os.name == "nt", "Windows Bash selection")
    def test_windows_ignores_wsl_bash_on_path(self):
        from unittest.mock import patch
        launcher = self.fixture("Git Bash selection")
        with patch.object(shutil, "which", return_value=r"C:\Windows\System32\bash.exe") as lookup:
            result = self.run_launcher(str(launcher))
        lookup.assert_not_called()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((launcher.parent / "ran.txt").is_file())

    def test_desktop_location_is_passed_as_data(self):
        self.command("/tmp/installer.desktop")

    def test_special_path_characters_are_literal_and_bootstrap_runs(self):
        for name in ("folder with spaces", "folder's apostrophe", "folder $(printf INJECTED) `printf INJECTED`"):
            with self.subTest(name=name):
                launcher = self.fixture(name)
                result = self.run_launcher(str(launcher))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((launcher.parent / "ran.txt").read_text().strip(), "ran")
                self.assertNotIn("INJECTED", result.stdout)

    def test_bootstrap_failure_exit_status_is_preserved(self):
        launcher = self.fixture(exit_code=7)
        result = self.run_launcher(str(launcher))
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("Installation failed with status 7", result.stdout)

    def test_file_uri_is_decoded_before_locating_bootstrap(self):
        launcher = self.fixture("folder's spaced URI")
        result = self.run_launcher(launcher.as_uri())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((launcher.parent / "ran.txt").is_file())

    def test_remote_uri_is_rejected_without_running_bootstrap(self):
        self.fixture()
        result = self.run_launcher("https://example.invalid/installer.desktop")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.base.rglob("ran.txt")))


if __name__ == "__main__":
    unittest.main()
