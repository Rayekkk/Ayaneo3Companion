"""Fan changes stay isolated from real hardware and use an independent guard."""

import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import fan_backend


class FanBackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.hwmon_root = self.root / "hwmon"
        self.hwmon_root.mkdir()
        self.device = self.hwmon_root / "hwmon0"
        self.device.mkdir()
        for name, value in (("name", "ayaneo_ec"), ("fan1_input", "2400"),
                            ("pwm1", "128"), ("pwm1_enable", "2")):
            (self.device / name).write_text(value + "\n")
        self.lease_dir = self.root / "run"
        self.controller = fan_backend.FanController(self.hwmon_root, self.lease_dir)
        self.commands = ("systemd-run", "systemctl", sys.executable)
        available = mock.patch.object(self.controller, "_guard_commands", return_value=self.commands)
        available.start()
        self.addCleanup(available.stop)
        prepare = mock.patch.object(self.controller, "_prepare_lease_dir",
                                    side_effect=lambda: self.lease_dir.mkdir(exist_ok=True))
        prepare.start()
        self.addCleanup(prepare.stop)
        self.calls = []

        def command(args):
            self.calls.append(args)
            if args[0] == "systemd-run":
                # The independent guard is armed while hardware is still Auto.
                self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")

        runner = mock.patch.object(self.controller, "_run", side_effect=command)
        runner.start()
        self.addCleanup(runner.stop)

    def test_absent_hwmon_disables_feature_without_raw_ec_fallback(self):
        (self.device / "name").write_text("other_hwmon\n")
        self.assertFalse(self.controller.status()["supported"])
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.controller.set("manual", 80)
        self.assertEqual(self.calls, [])

    def test_ambiguous_hwmon_is_rejected(self):
        duplicate = self.hwmon_root / "hwmon1"
        duplicate.mkdir()
        (duplicate / "name").write_text("ayaneo_ec\n")
        self.assertFalse(self.controller.status()["supported"])
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            self.controller.set("manual", 80)

    def test_manual_is_armed_before_write_and_auto_releases_guard(self):
        status = self.controller.set("manual", 60)
        self.assertEqual(status["mode"], "manual")
        self.assertEqual(status["rpm"], 2400)
        self.assertEqual((self.device / "pwm1").read_text().strip(), "153")
        token = self.controller._token
        self.assertEqual(self.controller._owner.read_text().strip(), token)
        self.assertEqual(self.calls[0][0], "systemd-run")
        self.assertIn("--timer-property=AccuracySec=1s", self.calls[0])
        self.assertIn("--on-active=30s", self.calls[0])
        self.assertIn("--property=Restart=on-failure", self.calls[0])
        self.assertIn("--property=RestartSec=5s", self.calls[0])
        self.assertIn("--property=StartLimitIntervalSec=0", self.calls[0])
        self.assertTrue(self.controller.renew())
        self.assertEqual(self.calls[1][:2], ["systemctl", "restart"])
        status = self.controller.set("auto")
        self.assertEqual(status["mode"], "auto")
        self.assertFalse(self.controller._owner.exists())
        self.assertEqual(self.calls[-1][:2], ["systemctl", "stop"])

    def test_auto_status_and_restore_do_not_read_unavailable_manual_pwm(self):
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.device / "pwm1":
                raise OSError(errno.EIO, "Input/output error")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
            status = self.controller.status()
            self.assertTrue(status["supported"])
            self.assertEqual(status["mode"], "auto")
            self.assertIsNone(status["percent"])
            self.assertEqual(status["rpm"], 2400)
            restored = self.controller.set("auto")
            self.assertTrue(restored["supported"])
            self.assertEqual(restored["mode"], "auto")

    def test_manual_status_still_requires_pwm_readback(self):
        (self.device / "pwm1_enable").write_text("1\n")
        original = Path.read_text

        def read(path, *args, **kwargs):
            if path == self.device / "pwm1":
                raise OSError(errno.EIO, "Input/output error")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", autospec=True, side_effect=read):
            status = self.controller.status()
        self.assertFalse(status["supported"])
        self.assertIn("Input/output error", status["reason"])

    def test_manual_validation_rejects_unsafe_or_malformed_pwm(self):
        for value in (59, 101, True, 60.0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.controller.set("manual", value)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")

    def test_kernel_pwm_quantization_does_not_trigger_false_rollback(self):
        original = Path.write_text

        def write(path, value, *args, **kwargs):
            if path == self.device / "pwm1":
                requested = int(value.strip())
                value = str((255 * ((requested * 100) // 255)) // 100) + "\n"
            return original(path, value, *args, **kwargs)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=write):
            status = self.controller.set("manual", 61)
        self.assertEqual(status["mode"], "manual")
        self.assertEqual(status["percent"], 61)
        self.assertEqual((self.device / "pwm1").read_text().strip(), "155")

    def test_guard_failure_never_enables_manual(self):
        with mock.patch.object(self.controller, "_run", side_effect=RuntimeError("systemd unavailable")):
            with self.assertRaisesRegex(RuntimeError, "systemd unavailable"):
                self.controller.set("manual", 80)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_auto_remains_available_when_guard_commands_disappear(self):
        self.controller.set("manual", 80)
        with mock.patch.object(self.controller, "_guard_commands",
                               side_effect=RuntimeError("systemd unavailable")):
            self.controller.set("auto")
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_failed_manual_write_restores_auto(self):
        original = Path.write_text

        def write(path, value, *args, **kwargs):
            if path == self.device / "pwm1_enable" and value == "1\n":
                raise OSError("EC write rejected")
            return original(path, value, *args, **kwargs)

        with mock.patch.object(Path, "write_text", autospec=True, side_effect=write):
            with self.assertRaisesRegex(OSError, "EC write rejected"):
                self.controller.set("manual", 80)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_guard_process_restores_auto_after_backend_disappears(self):
        self.controller.set("manual", 80)
        token = self.controller._token
        result = subprocess.run([sys.executable, "-c", fan_backend._GUARD_SCRIPT,
                                 str(self.controller._owner), str(self.hwmon_root), token],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_guard_retries_after_a_long_sysfs_outage(self):
        self.controller.set("manual", 80)
        token = self.controller._token
        self.device.rename(self.hwmon_root / "offline")
        command = [sys.executable, "-c", fan_backend._GUARD_SCRIPT,
                   str(self.controller._owner), str(self.hwmon_root), token]
        # The first service run exhausts its five attempts. systemd must
        # restart it; the lease is intentionally preserved for that retry.
        first = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(self.controller._owner.read_text().strip(), token)
        (self.hwmon_root / "offline").rename(self.device)
        second = subprocess.run(command, capture_output=True, text=True, timeout=8)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())

    def test_old_guard_cannot_change_new_session(self):
        self.controller.set("manual", 80)
        self.controller._owner.write_text("new-session\n")
        result = subprocess.run([sys.executable, "-c", fan_backend._GUARD_SCRIPT,
                                 str(self.controller._owner), str(self.hwmon_root),
                                 self.controller._token],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")
        self.assertEqual(self.controller._owner.read_text().strip(), "new-session")

    def test_restart_recovers_only_a_prior_owned_session(self):
        self.controller.set("manual", 80)
        replacement = fan_backend.FanController(self.hwmon_root, self.lease_dir)
        replacement.recover_abandoned()
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(self.controller._owner.exists())
        (self.device / "pwm1_enable").write_text("1\n")
        replacement.recover_abandoned()
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")

    def test_zero_rpm_ends_manual_session(self):
        self.controller.set("manual", 60)
        (self.device / "fan1_input").write_text("0\n")
        with self.assertRaisesRegex(RuntimeError, "RPM is zero"):
            self.controller.renew()
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")


if __name__ == "__main__":
    unittest.main()
