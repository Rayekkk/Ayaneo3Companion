# SPDX-License-Identifier: BSD-3-Clause
"""Failure recovery across map files, controller state and delayed boot work."""
import asyncio
import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_logic
import main
from safe_settings import SettingsManager


class ControlTransactionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.target = self.root / 'maps/ayaneo_type7.yaml'
        self.target.parent.mkdir()
        self.legacy = self.root / 'maps/legacy.yaml'
        self.data = b'# Managed by AYANEO 3 Companion\nkind: Test\n'
        self.store = SettingsManager('settings', str(self.root / 'settings'))
        self.store.replace({'tm_guard_enabled': True, 'cpu_boost': False})
        state = {'supported': True, 'controller': dict(main.DEFAULT_CONTROLLER),
                 'button_fix_installed': False, 'tm_guard_enabled': True,
                 'tm_guard_status': 'Monitoring', 'modules_reconnecting': False,
                 'modules_detached': False, 'tdp': dict(main.DEFAULT_TDP)}
        for name, value in [('settings', self.store), ('INPUT_MAP_TARGET', self.target),
                            ('LEGACY_INPUT_DEVICE_TARGET', self.legacy), ('LEGACY_INPUT_MAP_TARGETS', ())]:
            self.stack.enter_context(patch.object(main, name, value))
        self.stack.enter_context(patch.object(main.Plugin, '_state', state))
        self.stack.enter_context(patch.object(main.Plugin, '_snapshot', side_effect=lambda: copy.deepcopy(main.Plugin._state)))
        self.stack.enter_context(patch.object(main, 'button_map_owned',
            side_effect=lambda path: path.is_file() and path.read_bytes().startswith(b'# Managed')))
        self.program = self.stack.enter_context(patch.object(main, 'program_rear_buttons'))
        self.restart = self.stack.enter_context(patch.object(main, '_systemctl'))
        self.install = self.stack.enter_context(patch.object(main, 'install_button_fix',
            side_effect=lambda: self.target.write_bytes(self.data)))
        self.remove = self.stack.enter_context(patch.object(main, 'remove_button_fix',
            side_effect=lambda: self.target.unlink(missing_ok=True)))
        self.plugin = main.Plugin()

    def test_failed_enable_restores_firmware_and_removes_new_map(self):
        self.restart.side_effect = [OSError('service unavailable'), None]
        with self.assertRaisesRegex(RuntimeError, 'previous bindings restored'):
            asyncio.run(self.plugin.set_button_fix(True))
        self.assertFalse(self.target.exists())
        self.assertEqual([call.args[0] for call in self.program.call_args_list], [True, False])
        self.assertFalse(main.Plugin._state['button_fix_installed'])

    def test_failed_disable_restores_exact_previous_map_and_enabled_bindings(self):
        old = self.data + b'old-extension: preserved\n'
        self.target.write_bytes(old)
        main.Plugin._state['button_fix_installed'] = True
        def remove():
            self.target.unlink()
            raise OSError('interrupted map removal')
        self.remove.side_effect = remove
        with self.assertRaisesRegex(RuntimeError, 'previous bindings restored'):
            asyncio.run(self.plugin.set_button_fix(False))
        self.assertEqual(self.target.read_bytes(), old)
        self.assertEqual([call.args[0] for call in self.program.call_args_list], [False, True])
        self.assertTrue(main.Plugin._state['button_fix_installed'])

    def test_foreign_override_is_rejected_before_touching_firmware(self):
        self.target.write_bytes(b'user override')
        with self.assertRaisesRegex(RuntimeError, 'another aya7'):
            asyncio.run(self.plugin.set_button_fix(True))
        self.program.assert_not_called()
        self.restart.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'user override')

    def test_failed_runtime_rollback_is_not_reported_as_restored(self):
        self.restart.side_effect = OSError('service unavailable')
        with self.assertRaisesRegex(RuntimeError, 'rollback failed: InputPlumber'):
            asyncio.run(self.plugin.set_button_fix(True))
        self.assertFalse(self.target.exists())

    def test_delayed_module_power_rechecks_eject_intent_under_controller_lock(self):
        started = threading.Event()
        results = []
        def worker():
            started.set()
            results.append(self.plugin._power_modules_if_allowed())
        with patch.object(main, 'both_modules_connected') as present, \
             patch.object(main, 'set_controller_power') as power:
            with main._controller_apply_lock:
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(started.wait(1))
                main.Plugin._state['modules_reconnecting'] = True
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [False])
        present.assert_not_called()
        power.assert_not_called()

    def test_controller_monitors_do_not_write_during_deliberate_eject(self):
        main.Plugin._state['modules_reconnecting'] = True
        with patch.object(main, 'apply_controller') as apply, \
             patch.object(main, 'reconcile_controller') as reconcile:
            with self.assertRaises(RuntimeError):
                main.Plugin._restore_current_controller()
            with self.assertRaises(RuntimeError):
                main.Plugin._reconcile_current_controller(True, True)
        apply.assert_not_called()
        reconcile.assert_not_called()

    def test_tm_guard_failed_save_preserves_saved_and_running_choice(self):
        before = self.store.data
        with patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                asyncio.run(self.plugin.set_tm_guard(False))
        self.assertEqual(self.store.data, before)
        self.assertTrue(main.Plugin._state['tm_guard_enabled'])
        self.assertEqual(main.Plugin._state['tm_guard_status'], 'Monitoring')

    def test_boot_restore_uses_latest_cpu_setting_instead_of_old_snapshot(self):
        async def sleep(_delay):
            self.store.replace({'cpu_boost': True})
        with patch.object(main.asyncio, 'sleep', side_effect=sleep), \
             patch.object(main.Plugin, '_reapply_current_tdp'), \
             patch.object(main.Plugin, '_restore_current_controller'), \
             patch.object(main, 'write_cpu_boost') as write:
            asyncio.run(self.plugin._restore_hardware())
        write.assert_called_once_with(True)

    def test_late_audio_restore_does_not_reenable_disabled_fix(self):
        main.Plugin._state['audio_fix_enabled'] = False
        with patch.object(main, 'apply_audio_fix') as apply, \
             patch.object(main, 'audio_fix_ready') as ready:
            self.assertTrue(self.plugin._restore_audio_once())
        apply.assert_not_called()
        ready.assert_not_called()

    def test_calibration_summary_save_failure_reports_applied_hardware_truthfully(self):
        main.Plugin._state.update(audio_calibration_available=True,
                                  audio_calibration_last='previous summary')
        result = dict(timestamp='now', left=4000, right=4100, ambient=25,
                      backup='/saved/calibration-backup.bin')
        with patch.object(main, 'audio_fix_ready', return_value=True), \
             patch.object(main, 'perform_audio_recalibration', return_value=result) as calibrate, \
             patch.object(self.store, 'commit', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(RuntimeError, 'was applied, but its summary could not be saved'):
                asyncio.run(self.plugin.recalibrate_audio())
        calibrate.assert_called_once_with()
        self.assertEqual(main.Plugin._state['audio_calibration_last'], 'previous summary')
        self.assertNotIn('audio_calibration_last', self.store.data)
        self.assertIn('/saved/calibration-backup.bin', main.Plugin._state['audio_fix_error'])
