# SPDX-License-Identifier: BSD-3-Clause
"""Pure curve validation and asymmetric control; no hardware or Decky imports.

Ported from LegionGo2Companion's fan_policy.py. The AYANEO 3 EC takes a duty in
percent (pwm1), so curves are in native percent: measured, 10 % runs ≈712 RPM,
100 % ≈5540 RPM, and anything below 10 % stops the fan.
"""
import copy
import math
from collections import deque

MIN_PCT, MAX_PCT, PCT_STEP = 10, 100, 1
LAST_MIN_PCT = 40  # The 100°C point never runs slower than this.
STOP_SETTLE_S = 20  # A rotor coasting down from full speed can exceed 12 seconds.
HARD_MAX_C = 100.0
LEASE_S, SAMPLE_S = 3.0, 0.25
TACH_GRACE_S = 12  # Spin-up time before a nonzero target with a slow tach counts as stalled (tests shorten it).
RISE_CAP_PCT = 40  # The most one sample may raise the target.
FALL_STEP_PCT = 5  # One descent step, at most every 2 s after the dwell.
# Measured on the console: 10 % ≈712 RPM and every further percent ≈53.6 RPM.
RPM_AT_MIN, RPM_PER_PCT = 712, (5540 - 712) / 90
PRESETS = ('silent', 'balanced', 'performance', 'custom')
# Starting points for editing, not firmware profiles or saved template identities
# (LegionGo2Companion's, as the same share of the fan's top speed).
TEMPLATES = {
    'silent': [[0, 0], [35, 0], [45, 31], [55, 38], [65, 50], [75, 69], [85, 88], [100, 100]],
    'balanced': [[0, 31], [35, 31], [45, 38], [55, 50], [65, 63], [75, 77], [85, 92], [100, 100]],
    'aggressive': [[0, 42], [35, 48], [45, 58], [55, 71], [65, 83], [75, 94], [85, 100], [100, 100]],
}
DEFAULT_CURVE = copy.deepcopy(TEMPLATES['balanced'])
SCHEMA = 1


def valid_pct(pct):
    return type(pct) is int and (pct == 0 or MIN_PCT <= pct <= MAX_PCT and pct % PCT_STEP == 0)


def expected_rpm(pct):
    """The tach speed a healthy fan reaches at `pct` (0 for a stop)."""
    return 0 if pct <= 0 else RPM_AT_MIN + (pct - MIN_PCT) * RPM_PER_PCT


def stop_timed_out(rpm, elapsed):
    return elapsed > STOP_SETTLE_S and rpm != 0


def validate_curve(value):
    if not isinstance(value, list) or len(value) != 8:
        raise ValueError('A curve must contain exactly eight points.')
    last_t, last_p = -1, 0
    result = []
    for i, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError('Invalid curve point.')
        t, pct = point
        if type(t) is not int or type(pct) is not int:
            raise ValueError('Temperature and speed must be integers.')
        if not last_t < t <= 100 or not valid_pct(pct) or pct < last_p:
            raise ValueError('Curve points must be ordered and inside the validated operating range.')
        if i == 7 and (t != 100 or pct < LAST_MIN_PCT):
            raise ValueError(f'The last point must be at 100°C and run at least {LAST_MIN_PCT}%.')
        result.append([t, pct]); last_t, last_p = t, pct
    return result


def validate_settings(value):
    """Schema 1 only: anything else fails closed for review. Unknown keys are dropped."""
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != SCHEMA:
        raise ValueError('Unsupported fan settings schema; review before enabling.')
    if type(value.get('enabled')) is not bool or type(value.get('revision')) is not int or value['revision'] < 0:
        raise ValueError('Invalid fan settings intent.')
    mappings = value.get('presets')
    if not isinstance(mappings, dict) or set(mappings) - set(PRESETS):
        raise ValueError('Unknown Companion preset assignment.')
    assignment_mode = value.get('assign_tdp_presets', False)
    if type(assignment_mode) is not bool:
        raise ValueError('TDP curve assignments must be a boolean.')
    return dict(schema=SCHEMA, enabled=value['enabled'], revision=value['revision'],
                global_curve=validate_curve(value.get('global_curve')),
                presets={k: validate_curve(v) for k, v in mappings.items()},
                assign_tdp_presets=assignment_mode)


def defaults():
    return dict(schema=SCHEMA, enabled=False, assign_tdp_presets=False, revision=0,
                global_curve=copy.deepcopy(TEMPLATES['balanced']), presets={})


def target_for(curve, temperature):
    if not isinstance(temperature, (float, int)) or isinstance(temperature, bool) or not math.isfinite(temperature):
        raise ValueError('Invalid temperature sample.')
    if temperature >= HARD_MAX_C:
        return MAX_PCT
    if temperature <= curve[0][0]:
        return curve[0][1]
    for (a, p1), (b, p2) in zip(curve, curve[1:]):
        if temperature <= b:
            raw = p1 + (p2-p1)*(temperature-a)/(b-a)
            return max(MIN_PCT, int(math.ceil(raw/PCT_STEP)*PCT_STEP)) if raw > 0 else 0
    return curve[-1][1]


class CurveControl:
    """Rise ≤2 samples; descent needs 2°C hysteresis/5s dwell, then 5%/2s.

    Start at the requested curve speed; bounded tach verification detects failure.
    Zero is a real stop. At 100°C full speed bypasses every ramp/dwell.
    """
    def __init__(self, now, temperature, curve=DEFAULT_CURVE):
        self.started = now
        desired = target_for(curve, temperature)
        self.target = desired
        self.peak = temperature
        self.cool_since = None
        self.last_down = now
        self.settling = False
        self.history = deque([(now, self.target)])

    def retarget(self, temperature, now):
        # A user/preset curve change needs a bounded descent even at a stable
        # ambient temperature; ordinary thermal descent still uses hysteresis.
        self.settling = True
        self.peak = temperature
        self.cool_since = now
        self.last_down = now

    def _remember_target(self, now):
        if not self.history or self.history[-1][1] != self.target:
            self.history.append((now, self.target))
        while len(self.history)>1 and self.history[1][0] <= now-12:
            self.history.popleft()

    def force_max(self, now, temperature=HARD_MAX_C):
        # The target changes immediately; a healthy rotor still needs its
        # bounded acceleration window. Repeated hot samples cannot renew it.
        if self.target == 0:
            self.started = now
            self.history.clear()
        self.target = MAX_PCT
        self.peak = temperature
        self.cool_since = None
        self.settling = False
        self._remember_target(now)
        return self.target

    def step(self, curve, temperature, now):
        desired = target_for(curve, temperature)
        if temperature >= HARD_MAX_C:
            return self.force_max(now, temperature)
        if self.target == 0 and desired > 0:
            self.started = now
            self.target = desired
            self.peak = temperature
            self.settling = False
            self.history.clear()
            self.history.append((now, self.target))
        if desired > self.target:
            self.target = min(desired, self.target + RISE_CAP_PCT)
            self.peak = temperature
            self.cool_since = None
            self.settling = False
        elif desired < self.target:
            # Explicit curve changes may settle without a fictitious temperature fall.
            if temperature > self.peak+2 and desired > target_for(curve, self.peak):
                self.settling = False
            cooling = temperature <= self.peak-2 or self.settling
            if cooling:
                if self.cool_since is None:
                    self.cool_since = now
                if now-self.cool_since >= 5 and now-self.last_down >= 2:
                    next_target = max(desired, self.target-FALL_STEP_PCT)
                    self.target = 0 if next_target < MIN_PCT else next_target
                    self.last_down = now
                    if self.target == desired:
                        self.settling = False
                        self.peak = temperature
            else:
                self.cool_since = None
                self.peak = max(self.peak, temperature)
        else:
            self.cool_since = None
            self.peak = max(self.peak, temperature)
        self._remember_target(now)
        return self.target

    def is_stalled(self, rpm, now):
        # A higher target needs ramp time on every curve/preset change, not
        # only at initial startup. The rolling minimum remains bounded and
        # still catches a stopped/underperforming fan during repeated edits.
        required = max(MIN_PCT, min(target for _, target in self.history))
        return self.target > 0 and now-self.started > TACH_GRACE_S and rpm < .6*expected_rpm(required)
