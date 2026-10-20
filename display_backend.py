# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 OLED display: the managed gamescope definition and the published EDID's HDR peak."""

import hashlib
import math
import os
import stat
import time
from pathlib import Path

import decky

import companion_common
from companion_common import LOG


LUA_SOURCE = companion_common.PLUGIN_DIR / "assets" / "ayaneo.ayaneo3.oled.lua"
LUA_TARGET = Path("/etc/gamescope/scripts/00-gamescope/displays/ayaneo.ayaneo3.oled.lua")
DISPLAY_SCRIPT_MARKER = b"-- Managed by AYANEO 3 Companion\n"
DISPLAY_SCRIPT_BACKUP_ROOT = Path("/var/lib/ayaneo3-companion/display-definition")
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


def display_script_owned(path: Path) -> bool:
    data = companion_common.normalized_file_bytes(path)
    if data is None:
        return False
    return (data.startswith(DISPLAY_SCRIPT_MARKER) or
            hashlib.sha256(data).hexdigest() in LEGACY_DISPLAY_SCRIPT_SHA256)


def is_our_display_script(path: Path) -> bool:
    data = companion_common.normalized_file_bytes(path)
    source = companion_common.normalized_file_bytes(LUA_SOURCE)
    return data is not None and source is not None and data == source


def display_script_conflict(path: Path) -> bool:
    return path.is_symlink() or (path.exists() and not display_script_owned(path))


def _backup_display_script(data: bytes) -> Path:
    DISPLAY_SCRIPT_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
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
    backup = None
    if LUA_TARGET.exists() and not display_script_owned(LUA_TARGET):
        if not replace_existing:
            raise RuntimeError("display definition replacement requires confirmation")
        backup = _backup_display_script(LUA_TARGET.read_bytes())
    LUA_TARGET.parent.mkdir(parents=True, exist_ok=True)
    companion_common.atomic_write_bytes(LUA_TARGET, LUA_SOURCE.read_bytes())
    return backup


def remove_display_script() -> None:
    """Remove only the display definition owned by this plugin."""
    companion_common.require_supported_device("display definition")
    if LUA_TARGET.is_symlink():
        raise RuntimeError("refusing to remove a symlinked gamescope definition")
    if not LUA_TARGET.exists():
        return
    if not display_script_owned(LUA_TARGET):
        raise RuntimeError("another AYANEO 3 gamescope definition is installed")
    LUA_TARGET.unlink()


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
