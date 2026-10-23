# SPDX-License-Identifier: BSD-3-Clause
"""The AYANEO 3 fan through the kernel's ayaneo-ec hwmon device.

Replaces LegionGo2Companion's raw-port fan_ec.py with the same interface for its
guardian: `pwm1_enable` 2 is the EC's own curve, 1 is a manual duty in `pwm1`.
Measured on the console: a `pwm1` write is ignored while the EC is automatic, so
manual mode is selected first and the duty written at once; the EC keeps percent,
so `pwm1` reads back up to a few counts lower than written.
"""
import contextlib
import math
import os
import pathlib
import time

from fan_sleep import Retry

HWMON_ROOT = pathlib.Path('/sys/class/hwmon')
DMI_ROOT = pathlib.Path('/sys/class/dmi/id')
AUTOMATIC, MANUAL = 2, 1
# Readback rounding (the EC stores percent) stays within this many percent of the target.
READBACK_TOLERANCE_PCT = 2


def platform_check(root=DMI_ROOT):
    read = lambda name: (pathlib.Path(root)/name).read_text().strip()
    identity = dict(sys_vendor=read('sys_vendor'), product_name=read('product_name'))
    if identity != dict(sys_vendor='AYANEO', product_name='AYANEO 3'):
        raise RuntimeError('Fan Control supports only the AYANEO 3.')
    return identity


def find_hwmon(root=HWMON_ROOT):
    matches = [path for path in pathlib.Path(root).glob('hwmon*')
               if (path/'name').is_file() and (path/'name').read_text().strip() == 'ayaneo_ec']
    if len(matches) != 1:
        raise Retry('The ayaneo_ec fan interface is not available.' if not matches
                    else 'Ambiguous ayaneo_ec fan interfaces.')
    return matches[0]


def pwm_for(pct):
    return math.ceil(pct * 255 / 100)


def pct_for(pwm):
    return round(pwm * 100 / 255)


def same_target(override, expected):
    """`override` from status(); `expected` None for the EC's own curve or a duty in percent."""
    if expected is None or override is None:
        return override is expected
    return type(override) is int and abs(override - expected) <= READBACK_TOLERANCE_PCT


class EC:
    """One owner's view of the fan. `lock_path` is shared by every process that may write."""

    def __init__(self, lock_path, hwmon=None):
        self.path = pathlib.Path(hwmon) if hwmon is not None else find_hwmon()
        self.lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)

    def close(self):
        os.close(self.lock)

    @contextlib.contextmanager
    def transaction(self, timeout=0.3):
        import fcntl
        deadline = time.monotonic()+timeout
        while True:
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Fan transaction ownership timeout.')
                time.sleep(0.002)
        try:
            yield self
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def _read(self, leaf):
        try:
            return int((self.path/leaf).read_text().strip())
        except (OSError, ValueError) as exc:
            raise Retry(f'Fan {leaf} read failed.') from exc

    def _write(self, leaf, value):
        with open(self.path/leaf, 'w') as handle:
            handle.write(str(value))

    def status(self):
        """rpm, and the override: None on the EC's own curve, a duty in percent in manual mode."""
        mode, pwm, rpm = self._read('pwm1_enable'), self._read('pwm1'), self._read('fan1_input')
        if not 0 <= rpm <= 10000 or not 0 <= pwm <= 255:
            raise RuntimeError('Implausible fan telemetry.')
        # Any other mode (0 is full speed) is nobody's curve we know: it never matches a target.
        override = None if mode == AUTOMATIC else pct_for(pwm) if mode == MANUAL else f'mode {mode}'
        return dict(rpm=rpm, override=override, mode=mode, pwm=pwm)

    def plausible(self):
        """Read-only check before any write: two reads must agree on who drives the fan."""
        first, second = self.status(), self.status()
        if first['mode'] != second['mode']:
            raise RuntimeError('Fan mode readings are unstable; Fan Control stays on automatic.')
        return second

    def set_target(self, pct, stopped):
        if type(pct) is not int or not 0 <= pct <= 100:
            raise ValueError('Use stop or a supported fan duty.')
        if stopped():
            raise RuntimeError('Fan lease already withdrawn.')
        if self._read('pwm1_enable') != MANUAL:
            self._write('pwm1_enable', MANUAL)
        self._write('pwm1', pwm_for(pct))
        if not same_target(self.status()['override'], pct):
            raise RuntimeError('Fan target readback failed.')

    def release(self):
        if self._read('pwm1_enable') != AUTOMATIC:
            self._write('pwm1_enable', AUTOMATIC)
        if self._read('pwm1_enable') != AUTOMATIC:
            raise RuntimeError('Firmware automatic release not confirmed.')
