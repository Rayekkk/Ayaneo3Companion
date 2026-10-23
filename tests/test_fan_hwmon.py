# SPDX-License-Identifier: BSD-3-Clause
"""The ayaneo_ec hwmon fan: identity, discovery, percent conversion, writes and release."""
import pathlib
import tempfile
import unittest

import support  # noqa: F401  (the Decky harness before any backend import)
import fan_hwmon
from fan_hwmon import EC, find_hwmon, pct_for, platform_check, pwm_for, same_target
from fan_sleep import Retry


class FakeEC:
    """A hwmon directory whose pwm1 keeps percent like the real EC (readback a few counts low)."""

    def __init__(self, root, mode=2, pwm=201, rpm=0):
        self.path = pathlib.Path(root)/'hwmon5'
        self.path.mkdir()
        (self.path/'name').write_text('ayaneo_ec\n')
        for leaf, value in (('pwm1_enable', mode), ('pwm1', pwm), ('fan1_input', rpm)):
            (self.path/leaf).write_text(f'{value}\n')

    def get(self, leaf):
        return int((self.path/leaf).read_text())

    def set(self, leaf, value):
        (self.path/leaf).write_text(f'{value}\n')


class Writes(EC):
    """The EC with the console's behaviour: pwm1 is ignored in automatic and stored as whole percent."""

    def _write(self, leaf, value):
        if leaf == 'pwm1':
            if self._read('pwm1_enable') != fan_hwmon.MANUAL:
                return
            value = int(int(value)*100/255)*255//100
        super()._write(leaf, value)


class FanHwmonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)

    def ec(self, **state):
        fake = FakeEC(self.root, **state)
        return fake, self.bare(Writes, fake)

    @staticmethod
    def bare(cls, fake):
        # Without the flock file, which needs POSIX; test_transaction_excludes_a_second_owner covers it.
        ec = cls.__new__(cls); ec.path = fake.path
        return ec

    def test_only_the_ayaneo_3_is_admitted(self):
        dmi = self.root/'dmi'; dmi.mkdir()
        for vendor, product, ok in (('AYANEO', 'AYANEO 3', True), ('AYANEO', 'AYANEO 2S', False),
                                    ('LENOVO', 'AYANEO 3', False)):
            (dmi/'sys_vendor').write_text(vendor+'\n'); (dmi/'product_name').write_text(product+'\n')
            with self.subTest(vendor=vendor, product=product):
                if ok:
                    self.assertEqual(platform_check(dmi)['product_name'], 'AYANEO 3')
                else:
                    with self.assertRaises(RuntimeError):
                        platform_check(dmi)

    def test_discovery_needs_exactly_one_ayaneo_ec(self):
        with self.assertRaisesRegex(Retry, 'not available'):
            find_hwmon(self.root)
        other = self.root/'hwmon4'; other.mkdir(); (other/'name').write_text('k10temp\n')
        fake = FakeEC(self.root)
        self.assertEqual(find_hwmon(self.root), fake.path)
        second = self.root/'hwmon9'; second.mkdir(); (second/'name').write_text('ayaneo_ec\n')
        with self.assertRaisesRegex(Retry, 'Ambiguous'):
            find_hwmon(self.root)

    def test_percent_conversion_never_writes_less_than_asked(self):
        for pct in range(101):
            self.assertGreaterEqual(pwm_for(pct)*100/255, pct)
            self.assertEqual(pct_for(pwm_for(pct)), pct)
        self.assertEqual((pwm_for(0), pwm_for(10), pwm_for(100)), (0, 26, 255))

    def test_readback_matches_within_two_percent_and_automatic_only_matches_automatic(self):
        self.assertTrue(same_target(None, None))
        self.assertTrue(same_target(48, 50)); self.assertTrue(same_target(52, 50))
        for override, expected in ((47, 50), (None, 0), (0, None), ('mode 0', 100), (True, 1), (50.0, 50)):
            self.assertFalse(same_target(override, expected), (override, expected))

    def test_status_reports_automatic_manual_and_unknown_modes(self):
        fake, ec = self.ec(mode=2, pwm=201, rpm=3100)
        self.assertEqual(ec.status(), dict(rpm=3100, override=None, mode=2, pwm=201))
        fake.set('pwm1_enable', 1)
        self.assertEqual(ec.status()['override'], 79)
        fake.set('pwm1_enable', 0)
        self.assertEqual(ec.status()['override'], 'mode 0')
        for leaf, value in (('fan1_input', 10001), ('pwm1', 256), ('fan1_input', -1)):
            fake.set('pwm1_enable', 2); fake.set('fan1_input', 0); fake.set('pwm1', 0); fake.set(leaf, value)
            with self.subTest(leaf=leaf, value=value), self.assertRaisesRegex(RuntimeError, 'Implausible'):
                ec.status()
        fake.set(leaf, 0); (fake.path/'pwm1').write_text('garbage')
        with self.assertRaises(Retry):
            ec.status()

    def test_set_target_selects_manual_first_and_verifies_the_duty(self):
        fake, ec = self.ec(mode=2, pwm=201)
        for pct in (37, 10, 0, 100):
            ec.set_target(pct, lambda: False)
            self.assertEqual(fake.get('pwm1_enable'), 1)
            self.assertTrue(same_target(ec.status()['override'], pct), pct)

    def test_set_target_rejects_bad_duties_a_withdrawn_lease_and_a_failed_readback(self):
        fake, ec = self.ec()
        for pct in (-1, 101, 50.0, True, None):
            with self.assertRaises(ValueError):
                ec.set_target(pct, lambda: False)
        with self.assertRaisesRegex(RuntimeError, 'withdrawn'):
            ec.set_target(50, lambda: True)
        self.assertEqual(fake.get('pwm1_enable'), 2, 'nothing is written after a withdrawal')

        class Ignored(Writes):
            def _write(self, leaf, value):
                if leaf != 'pwm1':
                    super()._write(leaf, value)
        ignored = self.bare(Ignored, fake)
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            ignored.set_target(50, lambda: False)

    def test_release_returns_to_the_firmware_curve_and_verifies_it(self):
        fake, ec = self.ec(mode=1, pwm=128)
        ec.release()
        self.assertEqual(fake.get('pwm1_enable'), 2)
        ec.release()  # already automatic: nothing to write

        class Stuck(Writes):
            def _write(self, leaf, value):
                pass
        fake.set('pwm1_enable', 1)
        stuck = self.bare(Stuck, fake)
        with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
            stuck.release()

    def test_plausible_rejects_a_mode_change_between_two_reads(self):
        fake, ec = self.ec(mode=2)
        self.assertIsNone(ec.plausible()['override'])
        reads = iter((2, 1))
        original = ec._read
        ec._read = lambda leaf: next(reads) if leaf == 'pwm1_enable' else original(leaf)
        with self.assertRaisesRegex(RuntimeError, 'unstable'):
            ec.plausible()

    @unittest.skipUnless(hasattr(__import__('os'), 'fork'), 'flock')
    def test_transaction_excludes_a_second_owner(self):
        fake = FakeEC(self.root)
        ec, other = EC(self.root/'ec.lock', fake.path), EC(self.root/'ec.lock', fake.path)
        self.addCleanup(ec.close); self.addCleanup(other.close)
        with ec.transaction():
            with self.assertRaisesRegex(TimeoutError, 'ownership'):
                with other.transaction(timeout=0.01):
                    pass
        with other.transaction():
            pass


if __name__ == '__main__':
    unittest.main()
