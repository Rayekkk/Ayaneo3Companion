"""The independent guard and a new manual write share one lease transaction."""
from contextlib import ExitStack
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import fan_backend


class FanGuardRaceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.hwmon = self.root / "hwmon"
        self.device = self.hwmon / "hwmon0"
        self.device.mkdir(parents=True)
        for name, value in (("name", "ayaneo_ec"), ("pwm1_enable", "2"),
                            ("pwm1", "153"), ("fan1_input", "2400")):
            (self.device / name).write_text(value + "\n")
        self.controller = fan_backend.FanController(self.hwmon, self.root / "lease")
        self.stack.enter_context(patch.object(self.controller, "_prepare_lease_dir",
            side_effect=lambda: self.controller.lease_dir.mkdir(exist_ok=True)))
        self.stack.enter_context(patch.object(self.controller, "_guard_commands", return_value=("fake",) * 3))
        self.stack.enter_context(patch.object(self.controller, "_run"))
        self.controller.set("manual", 60)

    def guard(self):
        try:
            exec(fan_backend._GUARD_SCRIPT, {"__name__": "__main__"})
        except SystemExit as stopped:
            return stopped.code

    def arguments(self):
        return ["guard", str(self.controller._owner), str(self.hwmon), self.controller._token]

    def test_expiring_guard_cannot_remove_a_new_manual_lease(self):
        inspected, finish_guard, manual_started, manual_done = (threading.Event() for _ in range(4))
        results, errors = {}, []
        original = Path.read_text
        def read(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if (threading.current_thread().name == "expired-guard"
                    and path == self.device / "pwm1_enable" and result.strip() == "2"):
                inspected.set()
                if not finish_guard.wait(3):
                    raise TimeoutError("test did not release the guard")
            return result
        def expired_guard():
            try:
                results["guard"] = self.guard()
            except BaseException as error:
                errors.append(error)
        def manual():
            manual_started.set()
            try:
                results["manual"] = self.controller.set("manual", 80)
            except BaseException as error:
                errors.append(error)
            finally:
                manual_done.set()
        with patch.object(sys, "argv", self.arguments()), patch.object(Path, "read_text", new=read):
            guard = threading.Thread(target=expired_guard, name="expired-guard")
            writer = threading.Thread(target=manual)
            guard.start()
            try:
                self.assertTrue(inspected.wait(2))
                writer.start()
                self.assertTrue(manual_started.wait(1))
                wrote_before_guard_finished = manual_done.wait(0.15)
            finally:
                finish_guard.set()
                guard.join(3)
                if writer.ident is not None:
                    writer.join(3)
        self.assertFalse(guard.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(wrote_before_guard_finished, "new manual write must wait for the guard's lease cleanup")
        self.assertEqual(results["guard"], 0)
        self.assertEqual(results["manual"]["mode"], "manual")
        self.assertTrue(self.controller._owner.exists())
        self.assertTrue(self.controller.renew())
        # Simulate expiry of the newly armed timer: it still owns a valid lease.
        with patch.object(sys, "argv", self.arguments()):
            self.assertEqual(self.guard(), 0)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")

    def test_removed_lease_finishes_guard_without_restart_or_hardware_write(self):
        arguments = self.arguments()
        self.controller._owner.unlink()
        with patch.object(sys, "argv", arguments), patch("time.sleep") as sleep:
            self.assertEqual(self.guard(), 0)
        sleep.assert_not_called()
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")

    def test_separate_guard_process_waits_for_manual_transaction(self):
        with fan_backend._lease_lock(self.controller._owner):
            process = subprocess.Popen([sys.executable, "-u", "-c",
                "print('ready', flush=True)\n" + fan_backend._GUARD_SCRIPT,
                *self.arguments()[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.15)
                self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")
            except BaseException:
                process.kill()
                process.communicate()
                raise
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_auto_remains_possible_if_the_lease_lock_is_unavailable(self):
        with patch.object(fan_backend, "_lease_lock", side_effect=PermissionError("lock unavailable")):
            result = self.controller.set("auto")
        self.assertEqual(result["mode"], "auto")
        self.assertTrue(self.controller._owner.exists(), "retain the guard's lease for independent recovery")

    def stalled_guard(self, arguments):
        # Expire the guard's wait deterministically while another process owns
        # the real OS lock. No actual systemd service or hardware is involved.
        clock = ("import itertools, time\n"
                 "ticks = itertools.count(0, 6)\n"
                 "time.monotonic = lambda: next(ticks)\n"
                 "time.sleep = lambda seconds: None\n")
        return subprocess.run([sys.executable, "-c", clock + fan_backend._GUARD_SCRIPT,
                               *arguments[1:]], capture_output=True, text=True, timeout=10)

    def test_stalled_lock_holder_cannot_block_independent_auto_recovery(self):
        arguments = self.arguments()
        with fan_backend._lease_lock(self.controller._owner):
            result = self.stalled_guard(arguments)
            self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2",
                             "a stalled plugin must not lock the independent safety guard out of Auto")
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(self.controller._owner.exists(),
                            "retain the lease and retry while the manual transaction is unfinished")
            # The paused transaction can resume after the emergency Auto write.
            # Its later manual write must still have a guard waiting to retry.
            (self.device / "pwm1_enable").write_text("1\n")
        with patch.object(sys, "argv", arguments):
            self.assertEqual(self.guard(), 0)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_stalled_lock_does_not_let_an_old_guard_touch_a_new_owner(self):
        arguments = self.arguments()
        with fan_backend._lease_lock(self.controller._owner):
            self.controller._owner.write_text("replacement-session\n")
            result = self.stalled_guard(arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")
        self.assertEqual(self.controller._owner.read_text().strip(), "replacement-session")
