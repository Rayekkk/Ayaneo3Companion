# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 OLED display: the managed gamescope definition and the published EDID's HDR peak.

gamescope reads display scripts only when it starts. A SteamOS update replaces
/etc, and a definition reinstalled there after an update lands a second after
gamescope has started, so the panel stays washed out until Gaming Mode
restarts. Like LegionGo2Companion, the definition therefore lives in the
gamescope user's ~/.config/gamescope/scripts, which updates keep; root writes
it through display_script_io.py running as that user.
"""

import errno
import hashlib
import math
import os
import stat
import time
from pathlib import Path

import decky

import companion_common
import system_process
from companion_common import LOG


SCRIPT_NAME = "ayaneo.ayaneo3.oled.lua"
LUA_SOURCE = companion_common.PLUGIN_DIR / "assets" / SCRIPT_NAME
# shortcut: gamescope's user is the Decky user and reads ~/.config, not an
# XDG_CONFIG_HOME override; read /proc/<gamescope>/environ if a distro sets one.
_ACCOUNT = companion_common.DECKY_ACCOUNT
CONFIG_DIR = Path(_ACCOUNT["user_home"]) / ".config" if _ACCOUNT else Path.home() / ".config"
LUA_TARGET = CONFIG_DIR / "gamescope" / "scripts" / SCRIPT_NAME
# Root writes the user's file as that user; a test or unprivileged run writes directly.
SCRIPT_USER = ((_ACCOUNT["user_uid"], _ACCOUNT["user_gid"])
               if _ACCOUNT and getattr(os, "geteuid", lambda: -1)() == 0 else None)
SCRIPT_IO = companion_common.PLUGIN_DIR / "display_script_io.py"
# Where releases up to 1.2.0 installed it; an update wipes it, so it only migrates.
LEGACY_LUA_TARGET = Path("/etc/gamescope/scripts/00-gamescope/displays") / SCRIPT_NAME
# A profile SteamOS ships itself takes over, as in LegionGo2Companion's display_native.
NATIVE_LUA = Path("/usr/share/gamescope/scripts/00-gamescope/displays") / SCRIPT_NAME
MAX_SCRIPT_BYTES = 32 * 1024
DISPLAY_SCRIPT_MARKER = b"-- Managed by AYANEO 3 Companion\n"
DISPLAY_SCRIPT_BACKUP_ROOT = companion_common.BACKUP_ROOT / "display-definition"
LEGACY_DISPLAY_SCRIPT_SHA256 = frozenset({
    # Initial public Gamma 2.2 definition (Display-P3 primaries).
    "98de33235379f187b9d0eeb1460016b71fbc808c7cecff574774bbae64a130d4",
    # Pre-marker DXQ7D0023 calibration.
    "f52b721078df6336855c543da325a908477382cd8f313d395a7eabf1ad9f0b21",
})
PUBLISHED_EDID = (Path(companion_common.DECKY_ACCOUNT["user_home"]) / ".config/gamescope/edid.bin"
                  if companion_common.DECKY_ACCOUNT else None)
PUBLISHED_EDID_UID = companion_common.DECKY_ACCOUNT.get("user_uid")
EDID_TARGET_NITS = 800


def read_script(path: Path) -> bytes | None:
    """A bounded regular file, never through a final link; in the user's folder only theirs."""
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC", "O_BINARY"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SCRIPT_BYTES:
            return None
        if SCRIPT_USER is not None and Path(path).parent == LUA_TARGET.parent and info.st_uid != SCRIPT_USER[0]:
            return None  # Planted there by someone else: neither theirs nor ours.
        data = os.read(fd, MAX_SCRIPT_BYTES + 1)
        return data if len(data) <= MAX_SCRIPT_BYTES else None
    finally:
        os.close(fd)


def _normalized(path: Path) -> bytes | None:
    data = read_script(path)
    return None if data is None else data.replace(b"\r\n", b"\n")


def display_script_owned(path: Path) -> bool:
    data = _normalized(path)
    if data is None:
        return False
    return (data.startswith(DISPLAY_SCRIPT_MARKER) or
            hashlib.sha256(data).hexdigest() in LEGACY_DISPLAY_SCRIPT_SHA256)


def is_our_display_script(path: Path) -> bool:
    data = _normalized(path)
    source = companion_common.normalized_file_bytes(LUA_SOURCE)
    return data is not None and source is not None and data == source


def display_script_conflict(path: Path) -> bool:
    return path.is_symlink() or (path.exists() and not display_script_owned(path))


def native_profile_present() -> bool:
    try:
        return stat.S_ISREG(os.stat(NATIVE_LUA).st_mode)
    except OSError:
        return False


def _script_io(operation: str, data: bytes = b"") -> int:
    """display_script_io.py as the gamescope user; its exit code, 1 on failure."""
    uid, gid = SCRIPT_USER
    try:
        result = system_process.run([system_process.PYTHON, "-I", str(SCRIPT_IO), operation,
                                     str(CONFIG_DIR), SCRIPT_NAME], 10, stdin=None, input=data,
                                    capture_output=True, cwd="/", user=uid, group=gid, extra_groups=[])
    except (OSError, ValueError) as error:
        decky.logger.warning(f"{LOG} display script {operation} failed: {error}")
        return 1
    return result.returncode


def write_script(data: bytes) -> None:
    if SCRIPT_USER is None:
        LUA_TARGET.parent.mkdir(parents=True, exist_ok=True)
        companion_common.atomic_write_bytes(LUA_TARGET, data, 0o644)
    elif _script_io("write", data) != 0:
        raise RuntimeError(f"could not write {LUA_TARGET}")


def unlink_script() -> None:
    if SCRIPT_USER is None:
        LUA_TARGET.unlink(missing_ok=True)
    elif _script_io("unlink") not in (0, 2):
        raise OSError(errno.EIO, "could not remove the display definition", str(LUA_TARGET))


def _remove_legacy_script() -> None:
    """Drop the copy older releases kept in /etc, once the user copy is in place or off."""
    if not LEGACY_LUA_TARGET.is_symlink() and display_script_owned(LEGACY_LUA_TARGET):
        LEGACY_LUA_TARGET.unlink(missing_ok=True)


def _backup_display_script(data: bytes) -> Path:
    companion_common.private_dir(DISPLAY_SCRIPT_BACKUP_ROOT)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(data).hexdigest()[:12]
    target = DISPLAY_SCRIPT_BACKUP_ROOT / f"{LUA_TARGET.name}-{stamp}-{digest}.bak"
    with target.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(target, 0o600)
    if target.read_bytes() != data:
        raise RuntimeError("display definition backup verification failed")
    return target


def install_display_script(replace_existing: bool = False) -> Path | None:
    """Install or upgrade only a display definition owned by this plugin."""
    companion_common.require_supported_device("display definition")
    if LUA_TARGET.is_symlink():
        raise RuntimeError("refusing to replace a symlinked gamescope definition")
    if native_profile_present():
        # SteamOS describes this panel itself now; ours would only override it.
        remove_display_script()
        return None
    backup = None
    if LUA_TARGET.exists() and not display_script_owned(LUA_TARGET):
        if not replace_existing:
            raise RuntimeError("display definition replacement requires confirmation")
        existing = read_script(LUA_TARGET)
        if existing is None:
            raise RuntimeError("the existing display definition cannot be read safely")
        backup = _backup_display_script(existing)
    # Rewriting identical bytes would move the mtime restart_needed() compares.
    if not is_our_display_script(LUA_TARGET):
        write_script(LUA_SOURCE.read_bytes())
    _remove_legacy_script()
    return backup


def remove_display_script() -> None:
    """Remove only the display definition owned by this plugin.

    A definition Companion replaced (after the user confirmed) comes back from its
    backup, unless SteamOS now ships its own profile, which a user copy would override.
    """
    companion_common.require_supported_device("display definition")
    if LUA_TARGET.is_symlink():
        raise RuntimeError("refusing to remove a symlinked gamescope definition")
    if LUA_TARGET.exists():
        if not display_script_owned(LUA_TARGET):
            raise RuntimeError("another AYANEO 3 gamescope definition is installed")
        unlink_script()
        replaced = _replaced_definition()
        if replaced is not None and not native_profile_present():
            write_script(replaced)
    _remove_legacy_script()


def _replaced_definition() -> bytes | None:
    """The newest backed-up definition that is not one of Companion's own."""
    try:
        backups = sorted(DISPLAY_SCRIPT_BACKUP_ROOT.glob(f"{LUA_TARGET.name}-*.bak"))  # names sort by time
    except OSError:
        return None
    for path in reversed(backups):
        try:
            if path.is_symlink() or path.stat().st_size > MAX_SCRIPT_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        data = raw.replace(b"\r\n", b"\n")
        if not data.startswith(DISPLAY_SCRIPT_MARKER) and hashlib.sha256(data).hexdigest() not in LEGACY_DISPLAY_SCRIPT_SHA256:
            return raw
    return None


_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_gamescope_pid = None
_loaded_session = None  # gamescope start known to have loaded our definition


def gamescope_started() -> float | None:
    """Epoch time the running gamescope started (LegionGo2Companion's method), or None."""
    global _gamescope_pid

    def started(pid):
        with open(f"/proc/{pid}/comm") as handle:
            if handle.read().strip() != "gamescope-wl":
                return None
        # starttime (field 22) is fixed at fork, in ticks since boot.
        with open(f"/proc/{pid}/stat") as handle:
            return int(handle.read().rpartition(")")[2].split()[19]) / _CLK_TCK
    try:
        with open("/proc/uptime") as handle:
            boot = time.time() - float(handle.read().split()[0])
        candidates = ([_gamescope_pid] if _gamescope_pid else []) + os.listdir("/proc")
    except (OSError, ValueError, IndexError):
        return None
    for pid in candidates:
        if not pid.isdigit():
            continue
        try:
            value = started(pid)
        except (OSError, ValueError, IndexError):
            value = None
        if value is not None:
            _gamescope_pid = pid
            return boot + value
    _gamescope_pid = None
    return None


def remember_loaded(started: float | None = None) -> float | None:
    """Note whether this gamescope session loaded our definition, before migration removes a copy."""
    global _loaded_session
    started = gamescope_started() if started is None else started
    if started is not None:
        for path in (LUA_TARGET, LEGACY_LUA_TARGET):
            try:
                if is_our_display_script(path) and os.stat(path, follow_symlinks=False).st_mtime <= started:
                    _loaded_session = started
            except OSError:
                continue
    return started


def restart_needed(enabled: bool) -> bool:
    """True when the running gamescope did not load the definition the switch asks for."""
    started = remember_loaded()
    if started is None:
        return False  # Cannot tell, so do not ask.
    # The start time is recomputed each call and jitters by milliseconds (/proc/uptime
    # has 10 ms resolution); another session starts seconds apart.
    loaded = _loaded_session is not None and abs(_loaded_session - started) < 1.0
    return (not loaded and is_our_display_script(LUA_TARGET)) if enabled else loaded


def restart_session() -> tuple[bool, str]:
    """Restart Gaming Mode so gamescope loads the definition. (ok, note)"""
    if not _ACCOUNT:
        return False, "the Gaming Mode user is unknown"
    import pwd
    name = pwd.getpwuid(_ACCOUNT["user_uid"]).pw_name
    runtime = f"/run/user/{_ACCOUNT['user_uid']}"
    failures = []
    for argv, label in ((["/usr/bin/systemctl", "--user", "restart", "gamescope-session.target"], "systemctl"),
                        # Valve's own session switcher, in case the unit layout ever changes.
                        (["/usr/bin/steamos-session-select", "gamescope"], "steamos-session-select")):
        if not os.path.exists(argv[0]):
            failures.append(f"{label} not found")
            continue
        command = ["/usr/bin/runuser", "-u", name, "--", "/usr/bin/env", f"HOME={_ACCOUNT['user_home']}",
                   f"USER={name}", f"LOGNAME={name}", f"XDG_RUNTIME_DIR={runtime}",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus", *argv]
        try:
            result = system_process.run(command, 20, capture_output=True, text=True)
        except (OSError, system_process.subprocess.SubprocessError) as error:
            failures.append(f"{label}: {error}")
            continue
        if result.returncode == 0:
            return True, f"restarting via {label}"
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        failures.append(f"{label}: {tail[0] if tail else f'exited {result.returncode}'}")
    return False, "; ".join(failures) or "no restart method available"


def _cta_luminance_code(nits: float) -> int:
    return max(1, min(255, round(32 * math.log2(float(nits) / 50))))


def _max_luminance_offset(data: bytes) -> int | None:
    """Locate the CTA HDR static metadata MaxCLL byte; None when absent or malformed."""
    if len(data) < 256 or len(data) % 128:
        return None
    for start in range(128, len(data), 128):
        block = data[start:start + 128]
        if block[0] != 0x02:
            continue
        dtd_start = block[2] or 127
        if not 4 <= dtd_start <= 127:
            return None
        pos = 4
        while pos < dtd_start:
            header = block[pos]
            length = header & 0x1f
            end = pos + 1 + length
            if end > dtd_start:
                return None
            if header >> 5 == 7 and length >= 4 and block[pos + 1] == 6:
                return start + pos + 4
            pos = end
    return None


def patch_ayaneo_edid(data: bytes, nits: float = EDID_TARGET_NITS):
    """Return an AYANEO 3 EDID with CTA MaxCLL set to the advertised peak."""
    if (data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00" or
            data[8:12] != b"\x07\x21\x13\x01"):
        return None
    offset = _max_luminance_offset(data)
    if offset is None:
        return None
    result = bytearray(data)
    result[offset] = _cta_luminance_code(nits)
    start = offset - offset % 128
    result[start + 127] = (-sum(result[start:start + 127])) & 0xff
    return bytes(result)


def published_edid_nits(data: bytes):
    offset = _max_luminance_offset(data)
    if offset is None:
        return None
    code = data[offset]
    return 50 * (2 ** (code / 32)) if code else None


def read_published_edid() -> bytes:
    """Read a bounded regular file; a bad user-owned EDID must not stall QAM."""
    if PUBLISHED_EDID is None:
        return b""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(PUBLISHED_EDID, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or not 256 <= info.st_size <= 256 * 128 or info.st_size % 128):
            return b""
        data = os.read(fd, info.st_size)
        return data if len(data) == info.st_size else b""
    finally:
        os.close(fd)


def patch_published_edid() -> bool:
    """Safely normalize gamescope's user-owned EDID copy in place."""
    if PUBLISHED_EDID is None or PUBLISHED_EDID_UID is None:
        return False
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(PUBLISHED_EDID, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != PUBLISHED_EDID_UID
                or not 256 <= info.st_size <= 256 * 128 or info.st_size % 128):
            return False
        data = os.read(fd, info.st_size)
        patched = patch_ayaneo_edid(data)
        if patched is None:
            return False
        if patched != data:
            def write_bytes(payload: bytes) -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short published EDID write")
                    remaining = remaining[written:]
                os.ftruncate(fd, len(payload))
                os.fsync(fd)

            try:
                write_bytes(patched)
                os.lseek(fd, 0, os.SEEK_SET)
                if os.read(fd, len(patched)) != patched:
                    raise RuntimeError("published EDID readback mismatch")
            except Exception as error:
                try:
                    write_bytes(data)
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.read(fd, len(data)) != data:
                        raise RuntimeError("published EDID rollback readback mismatch")
                except Exception as recovery:
                    raise RuntimeError(f"{error}; EDID recovery failed: {recovery}") from error
                raise
            decky.logger.info(f"{LOG} normalized published EDID MaxCLL to {EDID_TARGET_NITS} nits")
        return True
    finally:
        os.close(fd)
