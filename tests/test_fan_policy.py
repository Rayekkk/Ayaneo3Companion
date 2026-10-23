# SPDX-License-Identifier: BSD-3-Clause
"""Curve validation and control, ported from LegionGo2Companion with the AYANEO 3's percent duty."""
import copy
import pathlib
import tempfile
import unittest

import support  # noqa: F401  (the Decky harness before any backend import)
from fan_policy import *  # noqa: F401,F403
from fan_policy import (CurveControl, DEFAULT_CURVE, LAST_MIN_PCT, MAX_PCT, MIN_PCT, PRESETS, TEMPLATES, SCHEMA,
                        defaults, expected_rpm, stop_timed_out, target_for, valid_pct, validate_curve, validate_settings)


class FanPolicyTests(unittest.TestCase):
    def test_templates_are_valid_ordered_starting_points_and_defaults_are_independent(self):
        self.assertEqual(set(TEMPLATES), {'silent', 'balanced', 'aggressive'})
        for curve in TEMPLATES.values():
            self.assertEqual(validate_curve(curve), curve)
        self.assertEqual(defaults()['global_curve'], TEMPLATES['balanced'])
        self.assertFalse(defaults()['assign_tdp_presets'])
        self.assertFalse(defaults()['enabled']); self.assertEqual(defaults()['presets'], {})
        self.assertNotIn('display_unit', defaults(), 'the curve is in percent; there is no unit to choose')
        original = copy.deepcopy(TEMPLATES)
        modified = defaults(); modified['global_curve'][0][1] = MAX_PCT
        self.assertEqual(TEMPLATES, original)
        for temp in range(101):
            self.assertLessEqual(target_for(TEMPLATES['silent'], temp), target_for(TEMPLATES['balanced'], temp))
            self.assertLessEqual(target_for(TEMPLATES['balanced'], temp), target_for(TEMPLATES['aggressive'], temp))

    def test_only_its_own_schema_loads_and_unknown_keys_are_dropped(self):
        for schema in (0, 2, 4, True, 1.0, '1', None):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                validate_settings(dict(defaults(), schema=schema))
        current = dict(defaults(), enabled=True, revision=7, presets={'custom': TEMPLATES['silent']})
        self.assertEqual(validate_settings(dict(current, display_unit='rpm', archive={})), current)
        for bad in (None, 1, 'false', [], {}):
            with self.assertRaises(ValueError):
                validate_settings(dict(current, assign_tdp_presets=bad))

    def test_eight_points_final_at_100_and_a_real_stop(self):
        self.assertEqual(validate_curve(DEFAULT_CURVE), DEFAULT_CURVE)
        for curve in [DEFAULT_CURVE[:-1], DEFAULT_CURVE+[DEFAULT_CURVE[-1]],
                      [[50, 0]]*8, [[50, MIN_PCT-1]]*8, [[50, True]]*8]:
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                validate_curve(curve)
        self.assertEqual(validate_curve([[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)])[0][1], 0)
        for index, axis, value in [(0, 0, -1), (7, 0, 99), (5, 0, DEFAULT_CURVE[4][0]), (7, 1, 0), (7, 1, 1),
                                   (7, 1, MIN_PCT-1), (7, 1, MAX_PCT+1), (7, 1, DEFAULT_CURVE[6][1]-1),
                                   (2, 0, True), (2, 1, float('nan')), (0, 1, 9), (0, 1, 5)]:
            bad = copy.deepcopy(DEFAULT_CURVE); bad[index][axis] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                validate_curve(bad)

    def test_below_ten_percent_only_a_stop_is_valid(self):
        # Measured: 5 % already stops the fan, and a restart is reliable from 10 %.
        self.assertTrue(valid_pct(0)); self.assertTrue(valid_pct(MIN_PCT)); self.assertTrue(valid_pct(MAX_PCT))
        for pct in (1, 5, 9, 101, -1, 10.0, True):
            self.assertFalse(valid_pct(pct), pct)

    def test_final_point_speed_is_editable_but_100c_still_forces_full_speed(self):
        for pct in (LAST_MIN_PCT, LAST_MIN_PCT+1, 70, MAX_PCT-1, MAX_PCT):
            curve = [[t, 0 if i < 7 else pct] for i, (t, _) in enumerate(DEFAULT_CURVE)]
            self.assertEqual(validate_curve(curve)[7], [100, pct])
        for pct in (0, MIN_PCT, LAST_MIN_PCT-1):
            with self.assertRaises(ValueError):
                validate_curve([[t, 0 if i < 7 else pct] for i, (t, _) in enumerate(DEFAULT_CURVE)])
        low = [[t, MIN_PCT] for t, _ in DEFAULT_CURVE[:7]]+[[100, LAST_MIN_PCT]]
        self.assertEqual(validate_curve(low), low)
        self.assertLessEqual(target_for(low, 99.9), LAST_MIN_PCT)
        for temp in (100, 100.5, 120):
            self.assertEqual(target_for(low, temp), MAX_PCT)
        p = CurveControl(0, 99, low)
        for tick in range(1, 41):
            self.assertLessEqual(p.step(low, 99, tick/4), LAST_MIN_PCT)
        self.assertEqual(p.step(low, 100, 10.25), MAX_PCT, 'hard maximum bypasses a 40 % final point')
        self.assertEqual(CurveControl(0, 100, low).target, MAX_PCT)

    def test_interpolation_never_requests_the_stop_deadband(self):
        stop_ramp = [[t, 0 if i < 2 else MIN_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        for c in [DEFAULT_CURVE, stop_ramp, [[t, MIN_PCT] for t, _ in DEFAULT_CURVE], [[t, MAX_PCT] for t, _ in DEFAULT_CURVE]]:
            targets = [target_for(c, t/10) for t in range(-100, 1501)]
            self.assertTrue(all(valid_pct(r) for r in targets))
            self.assertEqual(targets, sorted(targets))
        for temp in [True, float('inf'), float('nan'), None, '55']:
            with self.assertRaises(ValueError):
                target_for(DEFAULT_CURVE, temp)

    def test_start_is_direct_rise_is_fast_and_thermal_descent_is_damped(self):
        c = [[t, MIN_PCT] for t, _ in DEFAULT_CURVE]
        p = CurveControl(0, 50, c)
        values = [p.step(c, 50, i/4) for i in range(120)]
        self.assertEqual(set(values), {MIN_PCT})
        high = [[t, MAX_PCT] for t, _ in DEFAULT_CURVE]
        self.assertEqual(p.step(high, 80, 30), 50)
        self.assertEqual(p.step(high, 80, 30.25), 90)
        self.assertEqual(p.step(high, 80, 30.5), MAX_PCT)
        self.assertEqual(p.step(c, 79, 31), MAX_PCT)  # hysteresis
        self.assertEqual(p.step(c, 77, 32), MAX_PCT)
        self.assertEqual(p.step(c, 77, 36), MAX_PCT)  # dwell
        self.assertEqual(p.step(c, 77, 37), MAX_PCT-5)
        self.assertEqual(p.step(c, 77, 38), MAX_PCT-5)  # step-down interval
        self.assertEqual(p.step(c, 77, 39), MAX_PCT-10)

    def test_settings_schema_assignments_and_defaults_are_isolated(self):
        a, b = defaults(), defaults(); original = copy.deepcopy(b)
        a['global_curve'][0][1] = MAX_PCT
        self.assertEqual(b, original)
        self.assertEqual(b['schema'], SCHEMA)
        self.assertFalse(validate_settings(b)['enabled'])
        for value in [dict(b, schema=99), dict(b, enabled='false'), dict(b, revision=True),
                      dict(b, presets={'qam': DEFAULT_CURVE}), dict(b, presets={'silent': []}),
                      dict(b, presets={'max': DEFAULT_CURVE})]:
            with self.assertRaises(ValueError):
                validate_settings(value)
        all_presets = dict(b, presets={p: DEFAULT_CURVE for p in PRESETS})
        self.assertEqual(set(validate_settings(all_presets)['presets']), {'silent', 'balanced', 'performance', 'custom'})

    def test_edit_or_preset_lower_curve_eventually_applies_at_constant_temperature(self):
        p = CurveControl(0, 50)
        high = [[t, MAX_PCT] for t, _ in DEFAULT_CURVE]
        low = [[t, MIN_PCT] for t, _ in DEFAULT_CURVE]
        for tick in range(79, 84):
            p.step(high, 50, tick/4)
        self.assertEqual(p.target, MAX_PCT)
        p.retarget(50, 21)
        values = [p.step(low, 50, i/4) for i in range(84, 300)]
        self.assertEqual(values[0], MAX_PCT); self.assertEqual(values[-1], MIN_PCT)
        changes = [b-a for a, b in zip(values, values[1:]) if b != a]
        self.assertTrue(all(-5 <= c < 0 for c in changes))

    def test_zero_restart_follows_curve_and_no_hidden_thermal_handover(self):
        stop = [[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        p = CurveControl(0, 45, stop)
        self.assertEqual(p.step(stop, 55, 2), 0)
        self.assertEqual(p.step(stop, 85, 3), 0)
        self.assertEqual(p.step(stop, 100, 3.25), MAX_PCT)
        self.assertEqual(p.step(stop, 105, 3.5), MAX_PCT)
        self.assertEqual(CurveControl(0, 100, stop).target, MAX_PCT)
        self.assertEqual(p.step(stop, 99, 4), MAX_PCT)
        p = CurveControl(0, 45, stop)
        positive = [[t, MIN_PCT if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        self.assertEqual(p.step(positive, 50, 10), MIN_PCT)

    def test_full_temperature_range_equal_speed_and_one_percent_resolution(self):
        curve = [[0, 0], [1, 0], [2, 10], [3, 11], [40, 55], [80, 55], [99, 99], [100, 100]]
        self.assertEqual(validate_curve(curve), curve)
        self.assertEqual(target_for(curve, 0), 0)
        self.assertEqual(target_for(curve, 2), 10)
        self.assertEqual(target_for(curve, 100), 100)
        for index in range(1, 8):
            bad = copy.deepcopy(curve); bad[index][0] = bad[index-1][0]
            with self.assertRaises(ValueError):
                validate_curve(bad)
        for index in range(1, 7):
            bad = copy.deepcopy(DEFAULT_CURVE); bad[index][1] = bad[index-1][1]-1
            with self.assertRaises(ValueError):
                validate_curve(bad)

    def test_settings_without_later_keys_load_with_their_defaults(self):
        old = defaults(); del old['assign_tdp_presets']
        self.assertFalse(validate_settings(old)['assign_tdp_presets'])

    def test_curve_points_must_be_integer_pairs(self):
        for point in ([0.5, 30], (0, 30, 1), 5, 'ab', None):
            curve = copy.deepcopy(DEFAULT_CURVE); curve[0] = point
            with self.subTest(point=point), self.assertRaises(ValueError):
                validate_curve(curve)

    def test_speed_follows_the_line_between_curve_points(self):
        c = [[20, 30], [40, 50], [50, 60], [60, 70], [70, 80], [80, 88], [90, 94], [100, 100]]
        self.assertEqual(target_for(c, 10), 30)
        self.assertEqual(target_for(c, 30), 40)
        self.assertEqual(target_for(c, 45), 55)
        self.assertEqual(target_for(c, 99), 100, 'a fraction rounds up: never slower than the line')

    def test_cooling_on_a_curve_with_a_stop_reaches_a_real_stop_in_bounded_steps(self):
        c = TEMPLATES['silent']; p = CurveControl(0, 60, c); now = 0; targets = [p.target]
        for temperature in [60]*8+[50]*40+[40]*40+[30]*80:
            now += 0.25; targets.append(p.step(c, temperature, now))
        self.assertEqual(targets[-1], 0)
        self.assertTrue(all(valid_pct(t) for t in targets))
        self.assertTrue(all(a-b <= 5 or b == 0 for a, b in zip(targets, targets[1:])), targets)
        self.assertFalse(p.is_stalled(0, now+60), 'a requested stop is not a stalled fan')

    def test_a_one_degree_dip_never_slows_the_fan(self):
        c = TEMPLATES['silent']; p = CurveControl(0, 60, c); now = 0
        def hold(temperature, expected, seconds=10):
            nonlocal now
            for _ in range(seconds*4):
                now += 0.25; self.assertEqual(p.step(c, temperature, now), expected, (temperature, now))
        hold(59, 44)  # right after start (60 °C on the silent curve)
        p.force_max(now); hold(99, MAX_PCT)  # after the 100°C override
        p.retarget(80, now)  # a curve change settles without a temperature fall
        for _ in range(64):
            now += 0.25; p.step(c, 80, now)
        self.assertEqual(p.target, 79)
        hold(79, 79)

    def test_a_stopped_tach_counts_as_a_stall_only_after_twelve_seconds(self):
        p = CurveControl(0, 50)
        self.assertFalse(p.is_stalled(0, 12)); self.assertTrue(p.is_stalled(0, 12.01))

    def test_the_stall_check_uses_the_measured_rpm_of_the_requested_duty(self):
        self.assertEqual(expected_rpm(0), 0)
        self.assertEqual(round(expected_rpm(10)), 712)
        self.assertEqual(round(expected_rpm(100)), 5540)
        self.assertEqual(round(expected_rpm(50)), 2858)
        full = [[t, MAX_PCT] for t, _ in DEFAULT_CURVE]
        p = CurveControl(0, 50, full)
        self.assertTrue(p.is_stalled(.6*5540-1, 13))
        self.assertFalse(p.is_stalled(.6*5540, 13))

    def test_running_fan_new_higher_target_gets_ramp_grace_without_masking_a_stall(self):
        low = [[t, MIN_PCT] for t, _ in DEFAULT_CURVE]; high = [[t, MAX_PCT] for t, _ in DEFAULT_CURVE]
        p = CurveControl(0, 50, low)
        for i in range(120):
            p.step(low, 50, i/4)
        self.assertFalse(p.is_stalled(712, 30))
        p.retarget(50, 30); p.step(high, 50, 30); p.step(high, 50, 30.25)
        self.assertFalse(p.is_stalled(712, 30.25), 'a previously stable low speed may ramp up')
        self.assertTrue(p.is_stalled(0, 30.25), 'curve edits must not hide a stopped fan')
        for i in range(122, 177):
            p.step(high, 50, i/4)
        self.assertTrue(p.is_stalled(712, 44), 'high target must eventually attain speed')
        for i in range(177, 200):
            p.step(high, 50, i/4)
        self.assertLessEqual(len(p.history), 49)

    def test_hard_max_ramps_a_running_fan_without_masking_failure(self):
        curve = [[0, 30], [35, 30], [45, 30], [55, 30], [65, 30], [75, 30], [99, 30], [100, LAST_MIN_PCT]]
        validate_curve(curve)
        p = CurveControl(0, 99, curve)
        for tick in range(1, 121):
            p.step(curve, 99, tick/4)
        self.assertEqual(p.step(curve, 100, 30.25), MAX_PCT)
        running = round(expected_rpm(30))
        self.assertFalse(p.is_stalled(running, 30.25), 'a running rotor cannot accelerate in the write sample')
        self.assertTrue(p.is_stalled(0, 30.25), 'hard maximum must not hide an already stopped rotor')
        for tick in range(122, 174):
            self.assertEqual(p.step(curve, 100, tick/4), MAX_PCT)
        self.assertTrue(p.is_stalled(running, 43.25), 'repeated hot samples must not extend ramp grace')
        self.assertFalse(p.is_stalled(5540, 43.25))

    def test_hard_max_starts_a_stopped_fan_with_bounded_startup_grace(self):
        curve = [[0, 0], [35, 0], [45, 0], [55, 0], [65, 0], [75, 0], [99, 0], [100, LAST_MIN_PCT]]
        validate_curve(curve)
        p = CurveControl(0, 99, curve)
        for tick in range(1, 121):
            p.step(curve, 99, tick/4)
        self.assertEqual(p.step(curve, 100, 30.25), MAX_PCT)
        self.assertFalse(p.is_stalled(0, 30.25))
        for tick in range(122, 174):
            p.step(curve, 100, tick/4)
        self.assertTrue(p.is_stalled(0, 43.25), 'a failed hard-max start must be detected')

    def test_independent_hard_max_adoption_preserves_acceleration_and_start_grace(self):
        from fan_guardian import adopt_hard_max
        class Readback:
            def status(self):
                return dict(rpm=0, override=MAX_PCT-1, mode=1, pwm=253)  # the EC's rounding
        for pct in (0, 30):
            curve = [[0, pct], [35, pct], [45, pct], [55, pct], [65, pct], [75, pct], [99, pct], [100, MAX_PCT]]
            p = CurveControl(0, 99, curve)
            for tick in range(1, 121):
                p.step(curve, 99, tick/4)
            with self.subTest(pct=pct), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                (root/'hard-max').touch()
                (root/'owned-target.json').write_text('{"pct":100}')
                state = {}
                adopt_hard_max(Readback(), root, state, p, 30.25)
                self.assertEqual(state['target'], MAX_PCT)
                self.assertFalse((root/'hard-max').exists())
                running = round(expected_rpm(pct))
                self.assertFalse(p.is_stalled(running, 30.25))
                if pct:
                    self.assertTrue(p.is_stalled(0, 30.25))
                for tick in range(122, 174):
                    p.step(curve, 100, tick/4)
                self.assertTrue(p.is_stalled(running, 43.25))

    def test_flat_curve_startup_settles_while_temperature_increases(self):
        curve = [[t, 11 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        p = CurveControl(0, 50, curve)
        values = [p.step(curve, 50+min(i/4, 15), i/4) for i in range(180)]
        self.assertEqual(values[0], 11)
        self.assertEqual(values[-1], 11)
        self.assertTrue(all(b-a >= -5 for a, b in zip(values, values[1:])))

    def test_restart_uses_requested_speed_without_boost_or_dwell(self):
        stop = [[t, 0] for t, _ in DEFAULT_CURVE]
        for pct in (10, 11, 40, 80, MAX_PCT):
            curve = [[t, pct] for t, _ in DEFAULT_CURVE]
            curve[7][1] = max(pct, LAST_MIN_PCT)
            p = CurveControl(0, 40, stop)
            self.assertEqual(p.target, 0)
            self.assertEqual(p.step(curve, 40, 30), pct)
            for tick in range(1, 60):
                self.assertEqual(p.step(curve, 40, 30+tick/4), pct)

    def test_failed_start_is_bounded_and_edits_cannot_renew_grace(self):
        curve = [[t, MIN_PCT] for t, _ in DEFAULT_CURVE]
        stop = [[t, 0] for t, _ in DEFAULT_CURVE]
        for restart in (False, True):
            p = CurveControl(0, 40, stop if restart else curve)
            start = 30 if restart else 0
            p.step(curve, 40, start)
            self.assertFalse(p.is_stalled(0, start+12))
            for tick in range(1, 52):
                p.retarget(40, start+tick/4); p.step(curve, 40, start+tick/4)
            self.assertTrue(p.is_stalled(0, start+13))
            self.assertTrue(p.is_stalled(400, start+13))
            self.assertFalse(p.is_stalled(712, start+13))

    def test_a_stop_must_reach_zero_rpm_within_its_settle_time(self):
        self.assertFalse(stop_timed_out(1200, 20))
        self.assertTrue(stop_timed_out(1, 20.01))
        self.assertFalse(stop_timed_out(0, 120))


if __name__ == '__main__':
    unittest.main()
