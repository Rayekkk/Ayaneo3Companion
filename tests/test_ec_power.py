# SPDX-License-Identifier: BSD-3-Clause
"""Disk I/O must not stop the Decky event loop; all device access is mocked."""
import unittest
from unittest.mock import MagicMock, Mock, patch
from _harness import owner
import ec_backend


class RawEcPowerTests(unittest.TestCase):
    def setUp(self):
        self.ec = MagicMock()
        self.ec.__enter__.return_value = self.ec
        self.ec.write.return_value = 1
        self.path = Mock()
        self.path.open.return_value = self.ec
        for target, value in (("supported_device", True), ("ensure_charge_control", self.path)):
            fixture = patch.object(owner(target), target, return_value=value)
            fixture.start()
            self.addCleanup(fixture.stop)
        fixture = patch.object(ec_backend, "AYANEO_EC_ROOT", Mock(exists=Mock(return_value=False)))
        fixture.start()
        self.addCleanup(fixture.stop)
        self.on = bytes([ec_backend.EC_CONTROLLER_POWER_ON])
        self.off = bytes([ec_backend.EC_CONTROLLER_POWER_OFF])

    def test_raw_ec_power_write_requires_matching_readback(self):
        self.ec.read.side_effect = [self.off, self.on]
        ec_backend.set_controller_power(True)
        self.assertEqual(self.ec.read.call_count, 2)
        self.ec.write.assert_called_once_with(self.on)

    def test_raw_ec_ignored_power_write_reports_failure_and_restores_previous(self):
        self.ec.read.side_effect = [self.on, self.on, self.on]
        with self.assertRaisesRegex(RuntimeError, "did not retain"):
            ec_backend.set_controller_power(False)
        self.assertEqual([call.args[0] for call in self.ec.write.call_args_list], [self.off, self.on])

    def test_raw_ec_unreadable_previous_power_never_writes(self):
        self.ec.read.return_value = b""
        with self.assertRaisesRegex(RuntimeError, "could not read"):
            ec_backend.set_controller_power(False)
        self.ec.write.assert_not_called()

    def test_raw_ec_failed_power_recovery_is_reported(self):
        self.ec.read.side_effect = [self.on, self.on, self.off]
        with self.assertRaisesRegex(RuntimeError, "recovery failed.*readback mismatch"):
            ec_backend.set_controller_power(False)

    def test_raw_ec_power_rejects_non_boolean_before_write(self):
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            ec_backend.set_controller_power(1)
        self.ec.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
