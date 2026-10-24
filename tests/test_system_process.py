# SPDX-License-Identifier: BSD-3-Clause
"""External programs never inherit Decky's frozen runtime libraries."""
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import support  # noqa: F401  (the Decky harness before any backend import)
import main
import audio_backend
import ec_backend
import fan_backend
import tdp_backend
import system_process
from system_process import system_env

CLEAN = {"PATH": "/usr/bin", "DISPLAY": ":1", "XDG_RUNTIME_DIR": "/run/user/1000", "LC_ALL": "C"}


class SystemProcessTests(unittest.TestCase):
    def test_frozen_runtime_is_kept_in_parent_but_not_external_programs(self):
        env = {name: "/tmp/_MEI/bundled" for name in (
            "LD_LIBRARY_PATH", "LD_LIBRARY_PATH_ORIG", "LD_PRELOAD", "LD_AUDIT")}
        env.update(PATH="/usr/bin", DISPLAY=":1", XDG_RUNTIME_DIR="/run/user/1000")
        child = subprocess.CompletedProcess([], 0, "x 5869\n", "")
        with patch.dict(os.environ, env, clear=True), \
             patch.object(subprocess, "run", return_value=child) as run:
            ec_backend._upower_time_to_full("BAT0")
            tdp_backend._powerstation_active()
            main._systemctl("restart", "inputplumber")
            audio_backend._set_audio_control(0, "L0 DSP1 Firmware Load", "on")
            fan_backend.retire_legacy_guard()
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"], CLEAN)
                self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(len(run.call_args_list), 5)
            self.assertEqual(dict(os.environ), env)

    def test_ryzenadj_runs_with_system_libraries_only(self):
        """The pinned helper links only libpci and libudev: no bundled library path at all."""
        process = type("Process", (), {"returncode": 0, "communicate": lambda self, timeout: (b"", b"")})()
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI/bundled", "PATH": "/usr/bin"}, clear=True),              patch.object(subprocess, "Popen", return_value=process) as popen:
            tdp_backend._spawn_ryzenadj(["--info"], 3)
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/usr/bin", "LC_ALL": "C"})

    def test_run_closes_stdin_returns_failures_and_kills_at_the_timeout(self):
        code = 'import os, sys; sys.stdout.write(sys.stdin.read() + os.environ["LC_ALL"]); sys.exit(3)'
        result = system_process.run([sys.executable, "-c", code], 30, capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (3, "C"))
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            system_process.run([sys.executable, "-c", "import time; time.sleep(60)"], .5)
        self.assertLess(time.monotonic() - started, 30, "the child was killed, not waited for")

    @unittest.skipUnless(os.name == "posix", "SteamOS interpreter")
    def test_helpers_run_on_the_system_python(self):
        self.assertEqual(system_process.PYTHON, "/usr/bin/python3")

    def test_clean_runtime_and_fallback_path(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(system_env(), {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})


if __name__ == "__main__":
    unittest.main()
