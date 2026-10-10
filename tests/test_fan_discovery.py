# SPDX-License-Identifier: BSD-3-Clause
"""Unrelated hwmon hotplug cannot block AYANEO reads or the independent guard."""
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import fan_backend


class FanDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.hwmon = self.root / "hwmon"
        self.hwmon.mkdir()
        self.device = self.hwmon / "hwmon0"
        self.device.mkdir()
        for name, value in (("name", "ayaneo_ec"), ("fan1_input", "2400"),
                            ("pwm1", "204"), ("pwm1_enable", "1")):
            (self.device / name).write_text(value + "\n")
        self.other = self.hwmon / "hwmon1"
        self.other.mkdir()  # Hotplug left an entry whose name has disappeared.
        self.controller = fan_backend.FanController(self.hwmon, self.root / "lease")
        self.stack.enter_context(patch.object(self.controller, "_guard_commands", return_value=("fake",) * 3))
        self.stack.enter_context(patch.object(self.controller, "_run"))

    def test_unrelated_disappearing_hwmon_does_not_hide_live_fan_status(self):
        status = self.controller.status()
        self.assertTrue(status["supported"])
        self.assertEqual(status["mode"], "manual")
        self.assertEqual(status["rpm"], 2400)

    def test_auto_restore_is_possible_while_another_hwmon_is_unreadable(self):
        self.controller.set("auto")
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")

    def test_independent_guard_skips_unrelated_hwmon_and_restores_auto(self):
        owner = self.root / "fan-owner"
        owner.write_text("test-token\n")
        with patch.object(sys, "argv", ["guard", str(owner), str(self.hwmon), "test-token"]), \
             patch("time.sleep"):
            with self.assertRaises(SystemExit) as result:
                exec(fan_backend._GUARD_SCRIPT, {"__name__": "__main__"})
        self.assertEqual(result.exception.code, 0)
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "2")
        self.assertFalse(owner.exists())

    def test_two_verified_ayaneo_devices_remain_ambiguous(self):
        (self.other / "name").write_text("ayaneo_ec\n")
        self.assertFalse(self.controller.status()["supported"])
        with self.assertRaises(RuntimeError):
            self.controller.set("auto")
        self.assertEqual((self.device / "pwm1_enable").read_text().strip(), "1")
