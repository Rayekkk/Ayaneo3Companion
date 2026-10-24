# SPDX-License-Identifier: BSD-3-Clause
"""Real detached processes, IPC, SIGKILL and flock on a model hwmon directory, never hardware.

Ported from LegionGo2Companion: its raw EC port model becomes the ayaneo_ec
files, pwm1_enable (2 automatic, 1 manual) and pwm1. Windows skips these POSIX
cases. Mocks do not establish physical fan response, which has separate device evidence.
"""
import errno
import json
import os
import pathlib
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUNNING_CURVE = [[0, 30], [20, 30], [35, 30], [50, 30], [65, 30], [80, 35], [90, 55], [100, 100]]
AUTOMATIC_RPM = 2100  # The EC's own curve at the model's temperature.


def model_worker(root, ipc_fd):
    import fan_guardian as guard
    import fan_hwmon
    import fcntl
    guard.RUNTIME = root
    guard.platform_check = lambda: {}
    real_validate_curve = guard.validate_curve
    def stalled_validate_curve(curve):  # A live guardian that stalls inside a configure, e.g. under memory pressure.
        if (root/'configure-stall').exists(): time.sleep(float((root/'configure-stall').read_text()))
        return real_validate_curve(curve)
    guard.validate_curve = stalled_validate_curve
    real_atomic = guard.atomic
    def atomic(path, data):  # A full /run tmpfs: the session records cannot be written.
        if (root/'enospc').exists() and pathlib.Path(path).name in ('reason.json', 'last.json'):
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_atomic(path, data)
    guard.atomic = atomic
    real_touch = pathlib.Path.touch
    def touch(path, *args, **kwargs):  # A /run whose inodes are exhausted: no new stop marker.
        if path.name == 'stop' and (root/'enospc-stop').exists():
            raise OSError(errno.ENOSPC, 'No space left on device')
        return real_touch(path, *args, **kwargs)
    pathlib.Path.touch = touch
    from fan_sleep import Pause, Retry
    def check_owners():  # Ownership has separate pure checks.
        if (root/'owners-slow').exists(): raise Retry('Fan ownership scan exceeded its time budget.')
    guard.check_owners = check_owners
    def ownership():
        fd = os.open(root/'owner.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException: os.close(fd); raise RuntimeError('Another Fan Control instance')
        return fd
    guard.secure_root = ownership
    from fan_policy import expected_rpm
    def in_worker():
        return os.getpid() == int((root/'worker-pid').read_text())
    class ModelEC(fan_hwmon.EC):
        """The console's EC: pwm1 is ignored while automatic, the tach follows the duty at once."""
        writes = 0
        def hang(self):
            if (root/'hang-io').exists() and in_worker():
                time.sleep(20)  # Backup must kill only this worker, then take the lock.
        def _read(self, leaf):
            self.hang()
            if leaf == 'fan1_input':
                if (root/'forced-tach').exists(): return int((root/'forced-tach').read_text())
                if super()._read('pwm1_enable') == fan_hwmon.AUTOMATIC: return AUTOMATIC_RPM
                pct = fan_hwmon.pct_for(super()._read('pwm1'))
                return round(expected_rpm(pct)) if pct >= 10 else 0  # Instant synthetic response, not fan physics.
            return super()._read(leaf)
        def _write(self, leaf, value):
            self.hang()
            if leaf == 'pwm1' and super()._read('pwm1_enable') != fan_hwmon.MANUAL:
                return
            super()._write(leaf, value)
            for name, sig in (('kill-after-write', signal.SIGKILL), ('term-after-write', signal.SIGTERM)):
                if (root/name).exists() and in_worker():
                    ModelEC.writes += 1
                    if ModelEC.writes == int((root/name).read_text()):
                        os.kill(os.getpid(), sig)  # SIGKILL: no transaction finally.
        def release(self):  # A transient EC error for the first N releases.
            left = root/'release-fails'
            if left.exists() and int(left.read_text()) > 0:
                left.write_text(str(int(left.read_text())-1)); raise RuntimeError('Transient EC error')
            return super().release()
    guard.open_ec = lambda: ModelEC(root/'ec.lock', root/'hwmon')
    class Sensors:
        def __init__(self): pass
        def read(self):
            if (root/'invalid-sensor').exists(): raise RuntimeError('Invalid model sensor')
            if (root/'slow-sensor').exists(): raise Retry('Stale/slow APU temperature acquisition.')
            temp = int((root/'temperature').read_text()) if (root/'temperature').exists() else 100 if (root/'thermal').exists() else 45
            gpu = int((root/'gpu-temperature').read_text()) if (root/'gpu-temperature').exists() else temp-1
            stamp = time.monotonic()
            self.last_sample = dict(cpu_temperature=temp, gpu_temperature=gpu,
                                    sensor_started_at=stamp, sensor_finished_at=stamp, acquisition_ms=0)
            return {'k10temp': temp, 'amdgpu': gpu}
        def close(self): pass
    class Sleep:
        def __init__(self):
            if (root/'sleep-in-progress').exists(): raise OSError(errno.EINPROGRESS, 'Fan suspend protection is unavailable.')
            (root/('inhibit-'+str(os.getpid()))).touch()
        def check(self):
            if (root/'prepare-sleep').exists(): raise Pause('Suspend requested before sleep')
        def close(self):
            # Assert release happened before the synthetic delay FD is closed.
            if (root/'hwmon'/'pwm1_enable').read_text().strip() != '2': raise RuntimeError('Inhibitor closed before EC automatic')
            (root/('inhibit-'+str(os.getpid()))).unlink(missing_ok=True)
    import fan_policy
    from fan_policy import CurveControl, target_for, MIN_PCT
    production_grace = fan_policy.TACH_GRACE_S
    class TransportControl(CurveControl):
        """Bypass ramp only in explicit transport kill tests; never hardware."""
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if (root/'bridge-transport').exists(): self.target = MIN_PCT
        def step(self, curve, temperature, now):
            if (root/'bridge-transport').exists(): self.target = target_for(curve, temperature); return self.target
            return super().step(curve, temperature, now)
        def is_stalled(self, rpm, now):  # The real stall check with a 1 s spin-up grace instead of 12 s.
            fan_policy.TACH_GRACE_S = 1 if (root/'tach-grace-1s').exists() else production_grace
            return super().is_stalled(rpm, now)
    if (root/'stop-settle-1s').exists():  # The real stop check with a 1 s settle instead of 20 s.
        real_stop_timed_out = guard.stop_timed_out
        guard.stop_timed_out = lambda rpm, elapsed: real_stop_timed_out(rpm, elapsed+19)
    guard.Sensors = Sensors; guard.SleepSafety = Sleep; guard.CurveControl = TransportControl
    (root/'worker-pid').write_text(str(os.getpid()))
    ipc = socket.socket(fileno=ipc_fd)
    try: guard.serve(ipc)
    except Exception as exc:
        ipc.send(json.dumps(dict(kind='closed', active=False, automatic=False, error=str(exc), outcome=guard.outcome_of(exc))).encode())
    finally: ipc.close()


@unittest.skipUnless(sys.platform == 'linux' and hasattr(os, 'pidfd_open'), 'Linux process recovery test')
class FanProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ayaneo3-fan-model-')
        self.root = pathlib.Path(self.tmp.name)
        hwmon = self.root/'hwmon'; hwmon.mkdir()
        # The console after boot: the EC's own curve, with a stale manual duty of 79 % in pwm1.
        for leaf, value in (('name', 'ayaneo_ec'), ('pwm1_enable', '2'), ('pwm1', '201')):
            (hwmon/leaf).write_text(value+'\n')
        self.workers = []; self.sockets = []
        state = self.start()
        self.assertEqual(state['kind'], 'ready', state)
        self.initial = state

    def start(self, timeout=3):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        p = subprocess.Popen([sys.executable, '-u', __file__, '--worker', str(self.root), str(b.fileno())],
                             pass_fds=(b.fileno(),), start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        b.close(); self.workers.append(p); self.sockets.append(a)
        self.p, self.ipc = p, a
        return self.receive_kind('ready', timeout, allow_closed=True)

    def receive_kind(self, kind, timeout, allow_closed=False):
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            if select.select([self.ipc], [], [], .1)[0]:
                try: raw = self.ipc.recv(16384)
                except ConnectionResetError:
                    raise AssertionError('IPC reset: '+(self.root/'last.json').read_text())
                if not raw: break
                state = json.loads(raw)
                if state['kind'] == kind or allow_closed and state['kind'] == 'closed': return state
                if state['kind'] == 'closed': raise AssertionError('Controller closed: '+json.dumps(state))
        last = (self.root/'last.json').read_text() if (self.root/'last.json').exists() else ''
        raise AssertionError('Missing '+kind+': '+last)

    @staticmethod
    def until(condition, timeout, step=.05):
        """Poll a condition owned by another process (no event to wait on) up to `timeout` s."""
        end = time.monotonic()+timeout
        while not condition():
            if time.monotonic() >= end: return False
            time.sleep(step)
        return True

    def put(self, name, value):
        # Atomic like sysfs: write_text truncates first and the guardian may read an empty file.
        tmp = self.root/(name+'.tmp'); tmp.write_text(value+'\n'); os.replace(tmp, self.root/name)

    def configure(self, enabled=True, revision=1, curve=None):
        # A process/IPC fixture must not depend on the shipped Balanced template's
        # duty at the model's 45 degrees. Template contents have their own policy tests.
        self.ipc.send(json.dumps(dict(op='configure', enabled=enabled, curve=curve if curve is not None else RUNNING_CURVE,
                                      revision=revision)).encode())
        return self.receive_kind('configured', 2)

    def attribute(self, name):
        # The processes write the model files truncate-then-write; real sysfs has no empty window.
        for _ in range(100):
            text = (self.root/'hwmon'/name).read_text()
            if text.strip(): return int(text)
            time.sleep(.01)
        raise AssertionError(name+' stayed empty')

    def mode(self):
        return self.attribute('pwm1_enable')

    def override(self):
        """None on the EC's own curve, else the manual duty in percent."""
        import fan_hwmon
        return None if self.mode() == 2 else fan_hwmon.pct_for(self.attribute('pwm1'))

    def wait_release(self, timeout=5):
        import fcntl
        end = time.monotonic()+timeout
        while time.monotonic() < end:
            try:
                value = json.loads((self.root/'last.json').read_text())
                # Observe the mode under the same lock as real transactions;
                # the backup may be performing its fresh confirmation reads.
                with (self.root/'ec.lock').open('rb') as f:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    automatic = self.mode() == 2
                if self.p.poll() is not None and value.get('automatic') and value.get('reason') and automatic:
                    return value
            except (FileNotFoundError, json.JSONDecodeError, BlockingIOError, ValueError): pass
            time.sleep(.05)
        raise AssertionError('No automatic release')

    def tearDown(self):
        for sock in self.sockets: sock.close()
        for p in self.workers:
            try: p.wait(timeout=6)
            except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=3)
        # Backup may finish just after the worker's exit. Verify ownership is
        # actually free instead of deleting a live recovery process's files.
        import fcntl
        deadline = time.monotonic()+3
        with (self.root/'owner.lock').open('r+') as f:
            while True:
                try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() > deadline: self.fail('Recovery ownership leaked')
                    time.sleep(.05)
        self.tmp.cleanup()

    def test_ready_reports_the_firmware_curve_without_writing(self):
        self.assertIsNone(self.initial['override']); self.assertTrue(self.initial['automatic'])
        self.assertEqual(self.initial['rpm'], AUTOMATIC_RPM)
        self.assertEqual((self.root/'hwmon'/'pwm1').read_text().strip(), '201', 'no write before a configure')

    def test_controller_eof_pauses_and_releases(self):
        st = self.configure(); self.assertTrue(st['active']); self.assertEqual(st['target'], 30)
        self.assertEqual(st['override'], 30, 'enable ACK must contain fresh duty readback, not the stale 79 %')
        self.assertEqual(st['mode'], 1)
        self.ipc.close(); self.assertEqual(self.wait_release()['outcome'], 'pause')
        self.assertFalse((self.root/'last-fault.json').exists(), 'a pause is not a fault')

    def test_silent_plugin_keeps_control_without_any_lease(self):
        # Desktop/Gaming switches can stall the plugin for seconds; the guardian runs the curve itself.
        # Silence past the backup's progress lease proves no plugin-side lease exists.
        from fan_policy import LEASE_S
        self.configure(); end = time.monotonic()+LEASE_S+.5
        while time.monotonic() < end:
            if select.select([self.ipc], [], [], max(0, end-time.monotonic()))[0]:
                state = json.loads(self.ipc.recv(16384))
                self.assertNotEqual(state['kind'], 'closed', state)
        state = self.receive_kind('telemetry', 1.5)
        self.assertTrue(state['active']); self.assertEqual(state['override'], 30)
        self.assertIsNone(self.p.poll())

    def test_sigterm_mid_target_write_pauses_and_releases(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT
        self.configure(); (self.root/'term-after-write').write_text('1')
        self.ipc.send(json.dumps(dict(op='configure', enabled=True, curve=[[t, MAX_PCT] for t, _ in DEFAULT_CURVE], revision=2)).encode())
        st = self.wait_release(); self.assertEqual(st['outcome'], 'pause'); self.assertIn('SIGTERM', st['reason'])
        self.assertEqual(self.p.wait(timeout=1), 0)

    def test_sigterm_to_owner_and_backup_releases_and_leaves_no_process(self):
        st = self.configure(); backup = st['backup_pid']
        os.kill(self.p.pid, signal.SIGTERM); os.kill(backup, signal.SIGTERM)
        self.assertEqual(self.wait_release()['outcome'], 'pause')
        def gone():
            try: os.kill(backup, 0)
            except ProcessLookupError: return True
            return False
        self.assertTrue(self.until(gone, 3), 'Backup survived SIGTERM')

    def test_sleep_pause_then_a_new_session_rearms(self):
        self.configure(); (self.root/'prepare-sleep').touch()
        self.assertEqual(self.wait_release()['outcome'], 'pause')
        (self.root/'prepare-sleep').unlink()
        self.assertEqual(self.start()['kind'], 'ready')
        st = self.configure(); self.assertTrue(st['active']); self.assertEqual(st['override'], 30)

    def test_startup_ownership_budget_is_retry_and_writes_nothing(self):
        self.ipc.close(); self.wait_release(); (self.root/'owners-slow').touch()
        st = self.start(); self.assertEqual(st['kind'], 'closed'); self.assertEqual(st['outcome'], 'retry')
        self.assertIsNone(self.override())
        self.p.wait(timeout=3); self.assertFalse((self.root/st['session']).exists(), 'no empty session dir is left behind')

    def test_start_during_sleep_preparation_is_pause_not_retry(self):
        # Seen on the Legion: re-arm attempts while logind is suspending must not spend the retry budget.
        self.ipc.close(); self.wait_release(); (self.root/'sleep-in-progress').touch()
        st = self.start(); self.assertEqual(st['kind'], 'closed'); self.assertEqual(st['outcome'], 'pause', st)
        self.assertIsNone(self.override())

    def test_orphaned_manual_duty_or_unknown_mode_is_released_at_session_start(self):
        for mode in ('1', '0'):  # 1.2.x's manual duty; 0 is the EC's full speed.
            with self.subTest(mode=mode):
                self.ipc.close(); self.wait_release()
                (self.root/'hwmon'/'pwm1_enable').write_text(mode+'\n')
                st = self.start(); self.assertEqual(st['kind'], 'ready', st)
                self.assertIsNone(st['override']); self.assertTrue(st['automatic'])
                self.assertEqual(self.mode(), 2)

    def test_ec_worker_sigkill_is_recovered_by_independent_backup(self):
        st = self.configure(); self.assertNotEqual(st['backup_pid'], self.p.pid)
        backup_inhibitor = self.root/('inhibit-'+str(st['backup_pid']))
        # Each process holds its own sleep inhibitor: the owner's dies with the owner.
        self.assertTrue((self.root/('inhibit-'+str(self.p.pid))).exists()); self.assertTrue(backup_inhibitor.exists())
        os.kill(self.p.pid, signal.SIGKILL); self.assertEqual(self.wait_release()['outcome'], 'retry')
        self.assertTrue(self.until(lambda: not backup_inhibitor.exists(), 2), 'the backup closes its inhibitor only after the release')

    def released(self, timeout=5):
        return self.until(lambda: self.p.poll() is not None and self.override() is None, timeout)

    def test_unwritable_stop_marker_never_blocks_the_owners_own_release(self):
        self.configure(); (self.root/'enospc-stop').touch()
        self.ipc.close(); self.assertTrue(self.released(), 'the owner is the only writer it has to stop')

    def test_unwritable_stop_marker_never_blocks_release_after_the_owner_died(self):
        self.configure(); (self.root/'enospc-stop').touch()
        os.kill(self.p.pid, signal.SIGKILL); self.p.wait(timeout=2)
        self.until(lambda: self.override() is None, 5)
        self.assertIsNone(self.override(), 'a backup that saw the owner die releases without the marker')

    def test_backup_retries_after_a_transient_ec_error_although_the_owner_is_gone(self):
        self.configure(); (self.root/'release-fails').write_text('8')
        os.kill(self.p.pid, signal.SIGKILL); self.p.wait(timeout=2)
        self.until(lambda: self.override() is None, 6)
        self.assertIsNone(self.override(), 'killing an already dead owner (ESRCH) must not end recovery')
        self.assertEqual((self.root/'release-fails').read_text(), '0')

    def test_full_runtime_dir_never_blocks_the_release(self):
        self.configure(); (self.root/'enospc').touch()
        self.ipc.close(); self.released()
        self.assertIsNone(self.override(), 'session records that cannot be written never keep the manual duty')
        self.assertIsNotNone(self.p.poll())

    def test_stalled_transaction_owner_is_reaped_then_released(self):
        self.configure(); (self.root/'hang-io').touch()
        self.wait_release(timeout=7)
        self.assertEqual(self.p.wait(timeout=1), -signal.SIGKILL)

    def test_session_start_removes_dirs_left_by_killed_sessions(self):
        import fcntl
        st = self.configure(); os.kill(st['backup_pid'], signal.SIGKILL); os.kill(self.p.pid, signal.SIGKILL); self.p.wait(timeout=2)
        stale = self.root/self.initial['session']; self.assertTrue(stale.is_dir())
        with (self.root/'owner.lock').open('r+') as f:  # The killed backup's exit frees ownership.
            end = time.monotonic()+3
            while True:
                try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except BlockingIOError:
                    if time.monotonic() > end: self.fail('Killed backup still owns recovery')
                    time.sleep(.05)
        st = self.start(); self.assertEqual(st['kind'], 'ready', st)
        self.assertFalse(stale.exists()); self.assertTrue((self.root/st['session']).is_dir())

    def test_backup_death_causes_worker_to_release(self):
        st = self.configure(); os.kill(st['backup_pid'], signal.SIGKILL)
        st = self.wait_release(); self.assertIn('backup watchdog died', st['reason']); self.assertEqual(st['outcome'], 'retry')

    def test_invalid_sensor_fails_closed(self):
        self.configure(); (self.root/'invalid-sensor').touch(); self.assertEqual(self.wait_release()['outcome'], 'fault')

    def test_temperature_changes_keep_control_and_use_the_hotter_apu_channel(self):
        from fan_policy import DEFAULT_CURVE
        self.put('gpu-temperature', '85')
        state = self.configure(curve=DEFAULT_CURVE)
        self.assertTrue(state['active']); self.assertEqual(state['target'], 92)
        self.put('temperature', '72')
        self.put('gpu-temperature', '47')
        deadline = time.monotonic()+1.5
        while time.monotonic() < deadline:
            state = self.receive_kind('telemetry', 1.5)
            if state.get('temperatures') == {'k10temp': 72, 'amdgpu': 47}: break
        self.assertEqual(state['temperatures'], {'k10temp': 72, 'amdgpu': 47})
        self.assertTrue(state['active']); self.assertFalse(state['automatic'])
        self.assertFalse((self.root/'last-fault.json').exists())

    def test_a_foreign_duty_or_mode_is_a_fault_that_keeps_its_sample_and_releases(self):
        for leaf, value, key in (('pwm1', '128', 'pwm'), ('pwm1_enable', '0', 'mode')):  # Another writer's 50 %; the EC's full speed.
            with self.subTest(leaf=leaf):
                if leaf == 'pwm1_enable':
                    self.initial = self.start(); self.assertEqual(self.initial['kind'], 'ready')
                self.configure()
                self.put('hwmon/'+leaf, value)
                st = self.wait_release(); self.assertIn('outside its owner', st['reason']); self.assertEqual(st['outcome'], 'fault')
                record = json.loads((self.root/'last-fault.json').read_text())
                self.assertEqual(record['session'], self.initial['session']); self.assertEqual(record['stage'], 'ec_readback')
                self.assertEqual(record['sample']['cpu_temperature'], 45)
                self.assertEqual(record['sample'][key], int(value))
                self.assertIn('sensor_started_at', record['sample'])

    def test_100_hardmax_without_handover_or_heating_hardware(self):
        from fan_policy import DEFAULT_CURVE, MIN_PCT, LAST_MIN_PCT, MAX_PCT
        self.configure(curve=[[t, MIN_PCT] for t, _ in DEFAULT_CURVE[:7]]+[[100, LAST_MIN_PCT]]); (self.root/'thermal').touch()
        end = time.monotonic()+1.5
        while time.monotonic() < end:
            state = self.receive_kind('telemetry', 1)
            if state['target'] == MAX_PCT:
                self.assertTrue(state['active']); self.assertFalse(state['automatic']); self.assertEqual(state['override'], MAX_PCT); return
        self.fail('No hard maximum')

    def test_duplicate_owner_cannot_arm_or_steal_the_duty(self):
        active = self.configure(); first = self.p
        st = self.start(); self.assertEqual(st['kind'], 'closed'); self.assertIn('Another', st['error'])
        self.assertIsNone(first.poll())
        self.assertEqual(self.override(), active['override'], 'A second owner must not change the duty')

    def test_explicit_stop_is_not_native_auto_and_disable_releases(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT
        curve = [[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        st = self.configure(curve=curve)
        self.assertEqual(st['target'], 0); self.assertFalse(st['automatic'])
        self.assertEqual((st['override'], st['mode']), (0, 1), 'a stop is a manual zero duty, not the EC curve')
        st = self.receive_kind('telemetry', 1)
        self.assertEqual(st['override'], 0)
        self.assertEqual(st['rpm'], 0); self.assertTrue(st['stop_verified'])
        st = self.configure(False, 2); self.assertTrue(st['automatic']); self.assertIsNone(st['override']); self.assertFalse(st['stop_verified'])

    def test_repeated_saves_do_not_postpone_the_stop_check(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT
        self.ipc.close(); self.wait_release()
        (self.root/'stop-settle-1s').touch()
        self.assertEqual(self.start()['kind'], 'ready')
        self.put('forced-tach', '900')  # The rotor never stops.
        stop = [[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        self.configure(curve=stop)
        for revision in range(2, 10):  # Saves every 0.3 s for 2.4 s, well past the 1 s settle.
            time.sleep(.3)
            try: self.configure(curve=stop, revision=revision)
            except (AssertionError, OSError): break  # Closed by the stop check.
        else: self.fail('Repeated saves postponed the stop check')
        self.assertIn('Fan stop did not reach zero', self.wait_release()['reason'])

    def test_restart_at_minimum_never_commands_a_boost(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT, MIN_PCT
        stop = [[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        low = [[t, MIN_PCT if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]
        self.configure(curve=stop)
        self.configure(curve=low, revision=2)
        targets = []; end = time.monotonic()+1.5
        while time.monotonic() < end:
            st = self.receive_kind('telemetry', 1); targets.append(st['target'])
            self.assertIn(st['override'], (0, MIN_PCT))
        self.assertEqual(targets[-1], MIN_PCT)
        self.ipc.close(); self.wait_release()

    def failed_start(self, tach):
        from fan_policy import DEFAULT_CURVE, MAX_PCT, MIN_PCT
        (self.root/'tach-grace-1s').touch(); self.put('forced-tach', str(tach))
        st = self.configure(curve=[[t, MIN_PCT if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)])
        self.assertEqual(st['rpm'], tach); self.assertEqual(st['override'], MIN_PCT)
        end = time.monotonic()+15
        while self.p.poll() is None and time.monotonic() < end:
            if select.select([self.ipc], [], [], .1)[0]:
                try: raw = self.ipc.recv(16384)
                except ConnectionResetError: break
                if not raw: break
        result = self.wait_release()
        self.assertIn('stable running speed', result['reason']); self.assertEqual(result['outcome'], 'fault')

    def test_no_start_releases_even_with_healthy_lease(self): self.failed_start(0)

    def test_underperforming_start_releases_even_with_healthy_lease(self): self.failed_start(400)

    def test_stopped_controller_sigkill_recovers_native_auto(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT
        self.configure(curve=[[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)])
        os.kill(self.p.pid, signal.SIGKILL); self.wait_release()

    def test_stopped_fan_restarts_on_warming_without_dwell(self):
        from fan_policy import DEFAULT_CURVE, MAX_PCT, MIN_PCT
        self.configure(curve=[[t, 0 if i < 7 else MAX_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]); self.put('temperature', '99')
        end = time.monotonic()+1.5
        while time.monotonic() < end:
            st = self.receive_kind('telemetry', 1)
            if st['target'] > 0:
                self.assertGreaterEqual(st['override'], MIN_PCT); return
        self.fail('Warming stop did not restart')

    def test_stopped_frozen_worker_thermal_backup_releases(self):
        from fan_policy import DEFAULT_CURVE, LAST_MIN_PCT, MAX_PCT
        # The final point at its lowest speed must not weaken the backup's hard maximum.
        self.configure(curve=[[t, 0 if i < 7 else LAST_MIN_PCT] for i, (t, _) in enumerate(DEFAULT_CURVE)]); self.receive_kind('telemetry', 1)
        os.kill(self.p.pid, signal.SIGSTOP); self.put('temperature', '100')
        # Frozen worker cannot service heat; backup sets max before progress-lease recovery.
        self.assertTrue(self.until(lambda: self.override() == MAX_PCT, 1.5), 'Independent backup did not impose hard maximum')
        self.wait_release(); self.assertEqual(self.p.wait(timeout=1), -signal.SIGKILL)

    def test_prepare_sleep_releases_before_inhibitor_close(self):
        self.configure(); (self.root/'prepare-sleep').touch(); self.assertEqual(self.wait_release()['outcome'], 'pause')
        self.until(lambda: not list(self.root.glob('inhibit-*')), 1)
        self.assertEqual(list(self.root.glob('inhibit-*')), [])

    def crash_from_automatic(self, position):
        # The AYANEO hazard: manual mode first adopts the stale 79 % left in pwm1.
        (self.root/'kill-after-write').write_text(str(position))
        self.ipc.send(json.dumps(dict(op='configure', enabled=True, curve=RUNNING_CURVE, revision=1)).encode())
        self.wait_release(); self.assertEqual(self.p.wait(timeout=1), -signal.SIGKILL)

    def test_sigkill_right_after_selecting_manual_mode_is_released(self): self.crash_from_automatic(1)

    def test_sigkill_right_after_the_first_duty_write_is_released(self): self.crash_from_automatic(2)

    def crash_during_target_change(self, bridge):
        from fan_policy import DEFAULT_CURVE, MAX_PCT
        if bridge: (self.root/'bridge-transport').touch()
        self.configure(); (self.root/'kill-after-write').write_text('1')
        self.ipc.send(json.dumps(dict(op='configure', enabled=True, curve=[[t, MAX_PCT] for t, _ in DEFAULT_CURVE], revision=2)).encode())
        self.wait_release(); self.assertEqual(self.p.wait(timeout=1), -signal.SIGKILL)

    def test_sigkill_during_a_ramped_target_change_is_released(self): self.crash_during_target_change(False)

    def test_sigkill_during_a_direct_jump_to_maximum_is_released(self): self.crash_during_target_change(True)

    def test_stale_enable_after_disable_fails_closed(self):
        self.configure(); self.configure(False, 2)
        from fan_policy import DEFAULT_CURVE
        self.ipc.send(json.dumps(dict(op='configure', enabled=True, curve=DEFAULT_CURVE, revision=1)).encode())
        self.wait_release()
        self.assertIsNone(self.override())

    def real_client(self):
        # Standalone model invocation does not import Decky's real settings.
        if 'decky' not in sys.modules:
            sys.path.insert(0, str(ROOT/'tests'))
            import support  # noqa: F401  (the Decky harness before any backend import)
        import fan_backend
        client = fan_backend.Client(runtime=self.root)
        client.process = self.p; client.socket = self.ipc; self.ipc.setblocking(False)
        client._accept(self.initial)
        return client

    def test_slow_settings_save_beside_real_monitor_keeps_control_then_unload_releases(self):
        import asyncio
        import copy
        import fan_policy
        from unittest.mock import patch
        client = self.real_client()
        import fan_backend
        plugin = fan_backend.Plugin(); plugin._client = client; plugin._loaded = plugin._supported = True
        plugin._state = dict(fan_policy.defaults(), enabled=True)
        client.configure(True, fan_policy.DEFAULT_CURVE)
        plugin._applied = (True, json.dumps(fan_policy.DEFAULT_CURVE))
        def slow_save(value):
            time.sleep(fan_policy.LEASE_S+.5)  # Longer than the backup's progress lease.
            plugin._state = copy.deepcopy(value)
        async def run():
            plugin._task = asyncio.create_task(plugin._monitor())
            try:
                with patch.object(plugin, '_save', side_effect=slow_save):
                    # A preset curve with assignment off: saved, the running curve unchanged.
                    result = await plugin.save_curve('custom', fan_policy.TEMPLATES['silent'], 0)
                self.assertTrue(result['settings']['enabled'])
                self.assertTrue(result['telemetry']['active'])
                self.assertFalse(result['telemetry']['automatic'])
            finally: await plugin._unload()
        asyncio.run(run())
        self.assertTrue(client.snapshot()['automatic']); self.assertEqual(client.snapshot()['outcome'], 'pause')
        self.assertEqual(self.p.wait(timeout=1), 0)

    def ended(self, client):
        deadline = time.monotonic()+6
        while time.monotonic() < deadline:
            try: client.pump()
            except RuntimeError: break
            time.sleep(.05)
        else: self.fail('Session did not end')
        client.close()
        return client.snapshot()

    def test_client_loss_of_worker_recovers_exact_session_as_retry(self):
        client = self.real_client()
        from fan_policy import DEFAULT_CURVE
        client.configure(True, DEFAULT_CURVE)
        try:
            os.kill(self.p.pid, signal.SIGKILL)
            state = self.ended(client)
            self.assertFalse(state['active']); self.assertTrue(state['automatic'])
            self.assertIsNone(state['override']); self.assertEqual(state['automatic_reads'], 3)
            self.assertIn('owner died', state['error']); self.assertEqual(state['outcome'], 'retry')
            with self.assertRaises(RuntimeError): client.check()
            record = json.loads((self.root/'last-fault.json').read_text())
            self.assertEqual(record['session'], self.initial['session']); self.assertEqual(record['outcome'], 'retry')
        finally: client.close()

    def test_configure_readback_timeout_of_a_stalled_live_guardian_is_a_retry(self):
        import asyncio
        from unittest.mock import patch
        import fan_policy
        client = self.real_client()
        import fan_backend
        client.configure(True, fan_policy.DEFAULT_CURVE)
        plugin = fan_backend.Plugin(); plugin._client = client; plugin._loaded = plugin._supported = True
        plugin._state = dict(fan_policy.defaults(), enabled=True)
        # Past the 2 s readback deadline, inside the backup's 3 s lease: the guardian stays alive.
        self.put('configure-stall', '2.5')
        with self.assertRaises(TimeoutError): client.configure(True, fan_policy.TEMPLATES['aggressive'])
        with patch.object(fan_backend, 'release_orphan', side_effect=AssertionError('receipt proved automatic')):
            outcome, reason = asyncio.run(plugin._end_session(client.timeout))
        self.assertEqual((outcome, reason), ('retry', str(client.timeout)))
        self.assertTrue(client.snapshot()['automatic']); self.assertIsNotNone(self.p.poll())
        self.assertIsNone(self.override())

    def test_both_guardian_processes_killed_release_only_session_frees_the_duty_then_off(self):
        import asyncio
        from unittest.mock import patch
        import fan_policy
        client = self.real_client()
        import fan_backend
        stop = [[t, 0 if i < 7 else fan_policy.MAX_PCT] for i, (t, _) in enumerate(fan_policy.DEFAULT_CURVE)]
        st = client.configure(True, stop)
        plugin = fan_backend.Plugin(); plugin._client = client; plugin._loaded = plugin._supported = True
        plugin.BACKOFF_S, plugin.POLL_S = (.05,), .05  # The monitor's pacing is not under test, its decisions are.
        plugin._state = dict(fan_policy.defaults(), enabled=True, global_curve=stop); plugin._applied = (True, json.dumps(stop))
        os.kill(st['backup_pid'], signal.SIGKILL); os.kill(self.p.pid, signal.SIGKILL); self.p.wait(timeout=2)
        self.assertEqual(self.override(), 0, 'nothing released the forced stop')
        model = fan_backend.Client
        def fresh_session():
            # The release-only session, started on the model hwmon instead of /usr/bin/python3.
            fresh = model(runtime=self.root); st = self.start(); self.assertEqual(st['kind'], 'ready', st)
            fresh.process = self.p; fresh.socket = self.ipc; self.ipc.setblocking(False); fresh._accept(st)
            fresh.start = lambda: None
            return fresh
        saved = []
        async def run():
            with patch.object(plugin, '_save', side_effect=saved.append), patch.object(fan_backend, 'Client', side_effect=fresh_session):
                await asyncio.wait_for(plugin._monitor(), 20)
        asyncio.run(run())
        self.assertIsNone(self.override())
        self.assertEqual(len(self.workers), 2, 'one release-only session, no re-arm')
        self.assertIn('restored by a release-only session', plugin._error)
        self.assertFalse(saved[-1]['enabled']); self.assertIsNone(plugin._client)

    def test_unload_releases_before_a_slow_settings_operation_drains(self):
        import asyncio
        import copy
        import threading
        from unittest.mock import patch
        import fan_policy
        client = self.real_client()
        import fan_backend
        plugin = fan_backend.Plugin(); plugin._client = client; plugin._loaded = plugin._supported = True
        plugin._state = dict(fan_policy.defaults(), enabled=True)
        client.configure(True, fan_policy.DEFAULT_CURVE)
        entered = threading.Event(); release = threading.Event()
        def save(value):
            entered.set(); release.wait(7); plugin._state = copy.deepcopy(value)
        async def run():
            with patch.object(plugin, '_save', side_effect=save):
                request = asyncio.create_task(plugin.save_curve('custom', fan_policy.TEMPLATES['silent'], 0))
                while not entered.is_set(): await asyncio.sleep(.01)
                unload = asyncio.create_task(plugin._unload())
                try:
                    deadline = time.monotonic()+5
                    while self.p.poll() is None and time.monotonic() < deadline: await asyncio.sleep(.05)
                    self.assertIsNotNone(self.p.poll(), 'withdrawal must not wait for settings I/O')
                    self.assertIsNone(self.override())
                    self.assertFalse(unload.done())
                finally:
                    release.set(); await request; await unload
        asyncio.run(run())
        self.assertTrue(client.snapshot()['automatic'])
        self.assertTrue(plugin._state['enabled'], 'unload never writes the saved intent')

    def test_client_sensor_range_is_a_fault_and_sensor_timeout_is_a_retry(self):
        from fan_policy import DEFAULT_CURVE
        for marker, outcome, text in (('invalid-sensor', 'fault', 'Invalid model sensor'), ('slow-sensor', 'retry', 'Stale/slow')):
            with self.subTest(outcome=outcome):
                if outcome == 'retry':
                    self.initial = self.start(); self.assertEqual(self.initial['kind'], 'ready')
                client = self.real_client(); client.configure(True, DEFAULT_CURVE)
                try:
                    (self.root/marker).touch()
                    state = self.ended(client); (self.root/marker).unlink()
                    self.assertFalse(state['active']); self.assertTrue(state['automatic'])
                    self.assertIn(text, state['error']); self.assertEqual(state['outcome'], outcome)
                    with self.assertRaises(RuntimeError): client.configure(True, DEFAULT_CURVE)
                    self.assertIsNone(self.override())
                    record = json.loads((self.root/'last-fault.json').read_text())
                    self.assertIn(text, record['reason']); self.assertEqual(record['outcome'], outcome)
                finally: client.close()

    def test_fault_log_is_bounded_private_and_retains_first_cause(self):
        import fan_guardian
        from unittest.mock import patch
        with patch.object(fan_guardian, 'RUNTIME', self.root):
            fan_guardian.record_fault('session-x', 'first sensor fault '+'x'*1000, 'sensor', 'fault')
            fan_guardian.record_fault('session-x', 'later socket reset', 'ipc', 'retry')
        path = self.root/'last-fault.json'; record = json.loads(path.read_text())
        self.assertTrue(record['reason'].startswith('first sensor fault')); self.assertEqual(len(record['reason']), 512)
        self.assertEqual(record['stage'], 'sensor'); self.assertEqual(record['outcome'], 'fault')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertLess(path.stat().st_size, 8192)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker': model_worker(pathlib.Path(sys.argv[2]), int(sys.argv[3]))
    else: unittest.main()
