# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO fan control through the upstream ayaneo-ec hwmon interface.

Manual PWM has no hardware watchdog.  A transient systemd timer is armed
*before* every manual session, then renewed while Decky is healthy.  Its
command is self-contained, so removal or failure of this plugin cannot leave
the fan indefinitely in manual mode.
"""

from contextlib import contextmanager
import errno
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import system_process


HWMON_ROOT = Path("/sys/class/hwmon")
LEASE_DIR = Path("/run/ayaneo3-companion")
FAN_MIN_PERCENT = 60
FAN_GUARD_SECONDS = 30
FAN_RENEW_SECONDS = 10


@contextmanager
def _lease_lock(owner):
    """Serialize the independent guard and plugin across processes/lifetimes."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(owner.parent / "fan.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("fan lease lock is not a regular file")
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)
        yield
    finally:
        os.close(fd)

# This runs as a systemd transient service, independently of the Decky process
# and of the plugin's files.  The lease token prevents an old instance's timer
# from changing a newer instance's fan setting.
_GUARD_SCRIPT = """\
import errno, os, pathlib, stat, sys, time
owner = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
token = sys.argv[3]
for attempt in range(5):
    lock = None
    locked = False
    try:
        # Same file lock as _lease_lock; this service must stay self-contained
        # even when Decky removes the plugin files.
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
        lock = os.open(owner.parent / 'fan.lock', flags, 0o600)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise OSError('fan lease lock is not a regular file')
        deadline = time.monotonic() + 5
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    # A paused or stuck Decky process must not prevent Auto.
                    # Without the lock, retain the owner and keep retrying:
                    # that transaction can still resume with a manual write.
                    break
                time.sleep(0.025)
        if owner.read_text().strip() != token:
            sys.exit(0)
        matches = []
        for p in root.glob('hwmon*'):
            try:
                if (p / 'name').read_text().strip() == 'ayaneo_ec':
                    matches.append(p)
            except OSError:
                continue
        if len(matches) == 1:
            enable = matches[0] / 'pwm1_enable'
            if owner.read_text().strip() != token:
                sys.exit(0)
            enable.write_text('2\\n')
            if enable.read_text().strip() == '2' and locked:
                if owner.read_text().strip() == token:
                    owner.unlink()
                sys.exit(0)
    except FileNotFoundError:
        # A removed session is complete, not a reason for Restart=on-failure.
        # A missing hwmon with a live lease must still be retried.
        if not owner.exists():
            sys.exit(0)
    except (OSError, ValueError):
        pass
    finally:
        if lock is not None:
            os.close(lock)
    time.sleep(1)
sys.exit(1)
"""


class FanController:
    def __init__(self, hwmon_root=HWMON_ROOT, lease_dir=LEASE_DIR):
        self.hwmon_root = Path(hwmon_root)
        self.lease_dir = Path(lease_dir)
        self._lock = threading.RLock()
        self._token = None

    @property
    def _owner(self):
        return self.lease_dir / "fan-owner"

    def _hwmon(self):
        try:
            matches = []
            for path in self.hwmon_root.glob("hwmon*"):
                try:
                    if (path / "name").read_text().strip() == "ayaneo_ec":
                        matches.append(path)
                except OSError:
                    # An unrelated hwmon can disappear during enumeration.
                    # Only a verified AYANEO interface participates in control.
                    continue
        except OSError as error:
            raise RuntimeError(f"AYANEO fan discovery failed: {error}") from error
        if len(matches) != 1:
            raise RuntimeError("AYANEO EC fan interface is unavailable or ambiguous")
        path = matches[0]
        if not all((path / name).is_file() for name in ("fan1_input", "pwm1", "pwm1_enable")):
            raise RuntimeError("AYANEO EC fan interface is incomplete")
        return path

    @staticmethod
    def _guard_commands():
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            raise RuntimeError("fan control requires the root Decky backend on Linux")
        # Decky runs as root, so do not resolve executable names through an
        # inherited PATH that could contain user-writable directories.
        commands = tuple(Path("/usr/bin") / name for name in
                         ("systemd-run", "systemctl", "python3"))
        if not all(path.is_file() for path in commands):
            raise RuntimeError("the independent systemd fan guard is unavailable")
        return tuple(str(path) for path in commands)

    @staticmethod
    def _read_int(path, *, low, high):
        value = int(path.read_text().strip())
        if value < low or value > high:
            raise RuntimeError(f"unexpected AYANEO fan value in {path.name}")
        return value

    def status(self):
        """Read live state, never trusting a cached manual/automatic choice."""
        try:
            path = self._hwmon()
            self._guard_commands()
            mode = self._read_int(path / "pwm1_enable", low=1, high=2)
            # Auto mode does not expose a meaningful manual PWM value.  Some
            # AYANEO EC revisions return EIO for pwm1 while Auto and RPM work.
            pwm = self._read_int(path / "pwm1", low=0, high=255) if mode == 1 else None
            rpm = self._read_int(path / "fan1_input", low=0, high=65535)
            return {"supported": True,
                    "mode": "manual" if mode == 1 else "auto",
                    "percent": round(pwm * 100 / 255) if pwm is not None else None,
                    "rpm": rpm}
        except (OSError, ValueError, RuntimeError) as error:
            return {"supported": False, "mode": "auto", "percent": None,
                    "rpm": None, "reason": str(error)}

    def _run(self, args):
        try:
            result = system_process.run(args, 5, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"fan guard command failed: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown error").strip()[:300]
            raise RuntimeError(f"fan guard command failed: {detail}")

    def _prepare_lease_dir(self):
        self.lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.lease_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("fan guard lease directory is not root-owned and private")

    def _owner_matches(self, token):
        try:
            return self._owner.read_text().strip() == token
        except OSError:
            return False

    def _write_owner(self, token):
        self._prepare_lease_dir()
        fd, temporary = tempfile.mkstemp(prefix=".fan-owner-", dir=self.lease_dir)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(token + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._owner)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _unit(token):
        return f"ayaneo3-companion-fan-{token}"

    def _arm(self, token, commands):
        systemd_run, systemctl, python3 = commands
        if self._owner_matches(token):
            self._run([systemctl, "restart", self._unit(token) + ".timer"])
            return
        self._run([systemd_run, "--system", "--collect",
                   f"--unit={self._unit(token)}",
                   f"--on-active={FAN_GUARD_SECONDS}s",
                   "--timer-property=AccuracySec=1s",
                   "--property=Type=exec",
                   # An EC/sysfs outage can last longer than the guard's five
                   # quick attempts. Keep retrying independently of Decky
                   # until Auto is restored or this lease is superseded.
                   "--property=Restart=on-failure",
                   "--property=RestartSec=5s",
                   "--property=StartLimitIntervalSec=0",
                   python3, "-c", _GUARD_SCRIPT,
                   str(self._owner), str(self.hwmon_root), token])
        self._write_owner(token)

    def _release(self, token, commands=None):
        if self._owner_matches(token):
            self._owner.unlink(missing_ok=True)
        self._token = None
        if commands is not None:
            try:
                self._run([commands[1], "stop", self._unit(token) + ".timer"])
            except RuntimeError:
                # A stopped timer is harmless after its lease has been removed.
                pass

    def _write_mode(self, path, mode):
        (path / "pwm1_enable").write_text(f"{mode}\n")
        if self._read_int(path / "pwm1_enable", low=1, high=2) != mode:
            raise RuntimeError("AYANEO fan mode did not retain the setting")

    def _auto(self, path, commands=None):
        self._write_mode(path, 2)
        if self._token is not None:
            self._release(self._token, commands)

    def set(self, mode, percent=None):
        if mode not in ("auto", "manual"):
            raise ValueError("fan mode must be auto or manual")
        if mode == "manual" and (type(percent) is not int or
                                  not FAN_MIN_PERCENT <= percent <= 100):
            raise ValueError(f"manual fan speed must be {FAN_MIN_PERCENT}–100%")
        with self._lock:
            try:
                self._prepare_lease_dir()
                with _lease_lock(self._owner):
                    return self._set_locked(mode, percent)
            except (OSError, RuntimeError):
                if mode != "auto":
                    raise
                # Auto must remain possible even if the lease directory or
                # lock is unavailable. Retain its owner so the guard can retry.
                self._write_mode(self._hwmon(), 2)
                return self.status()

    def _set_locked(self, mode, percent=None):
        with self._lock:
            path = self._hwmon()
            if mode == "auto":
                try:
                    commands = self._guard_commands()
                except RuntimeError:
                    # Restoring the hardware default must remain possible even
                    # when systemd-run disappears after a manual session.
                    commands = None
                self._auto(path, commands)
                return self.status()

            commands = self._guard_commands()

            token = (self._token if self._token and self._owner_matches(self._token)
                     else secrets.token_hex(8))
            try:
                # The timer must exist before a fixed PWM can be enabled.
                self._arm(token, commands)
                self._token = token
                pwm = round(percent * 255 / 100)
                (path / "pwm1").write_text(f"{pwm}\n")
                # ayaneo-ec stores a 0..100 EC value, so the kernel truncates
                # twice when converting from and back to hwmon's 0..255 PWM.
                # A requested 61% (156) therefore reads back as 155.
                expected_readback = (255 * ((pwm * 100) // 255)) // 100
                if self._read_int(path / "pwm1", low=0, high=255) not in (
                        pwm, expected_readback):
                    raise RuntimeError("AYANEO fan PWM did not retain the setting")
                if not self._owner_matches(token):
                    raise RuntimeError("independent fan guard expired before manual mode was applied")
                self._write_mode(path, 1)
                result = self.status()
                if not result["supported"] or result["mode"] != "manual":
                    raise RuntimeError(result.get("reason", "AYANEO fan mode changed unexpectedly"))
                if not self._owner_matches(token):
                    raise RuntimeError("independent fan guard expired during manual mode change")
                return result
            except Exception as error:
                try:
                    self._auto(path, commands)
                except Exception as recovery:
                    # Preserve the timer lease so it can retry automatic mode.
                    raise RuntimeError(f"{error}; fan recovery failed: {recovery}") from error
                raise

    def renew(self):
        with self._lock:
            if self._token is None:
                return False
            self._prepare_lease_dir()
            with _lease_lock(self._owner):
                return self._renew_locked()

    def _renew_locked(self):
        """Keep manual mode only while Decky's loop and systemd both work."""
        with self._lock:
            token = self._token
            if not token or not self._owner_matches(token):
                self._token = None
                return False
            path = None
            commands = None
            try:
                path = self._hwmon()
                commands = self._guard_commands()
                if self._read_int(path / "pwm1_enable", low=1, high=2) != 1:
                    self._release(token, commands)
                    return False
                if self._read_int(path / "pwm1", low=0, high=255) < round(FAN_MIN_PERCENT * 255 / 100):
                    raise RuntimeError("fan PWM fell below the safe manual range")
                if self._read_int(path / "fan1_input", low=0, high=65535) == 0:
                    raise RuntimeError("fan RPM is zero in manual mode")
                self._arm(token, commands)
                return True
            except Exception:
                try:
                    if path is not None:
                        self._auto(path, commands)
                except Exception:
                    # Keep the lease; its independently scheduled timer remains
                    # the last chance to restore hardware automatic control.
                    pass
                raise

    def close(self):
        with self._lock:
            if self._token is None:
                return
            self._prepare_lease_dir()
            with _lease_lock(self._owner):
                self._close_locked()

    def _close_locked(self):
        """Return only this instance's manual session to hardware auto."""
        with self._lock:
            token = self._token
            if token is None or not self._owner_matches(token):
                self._token = None
                return
            try:
                commands = self._guard_commands()
            except RuntimeError:
                commands = None
            self._auto(self._hwmon(), commands)

    def recover_abandoned(self):
        with self._lock:
            if not self._owner.exists():
                return
            self._prepare_lease_dir()
            with _lease_lock(self._owner):
                self._recover_abandoned_locked()

    def _recover_abandoned_locked(self):
        """On Decky restart, repair an owned manual session immediately."""
        with self._lock:
            if not self._owner.exists():
                return
            self._write_mode(self._hwmon(), 2)
            self._owner.unlink(missing_ok=True)
            self._token = None
