# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""InputPlumber aya7 capability-map extension that exposes LC, RC, LC1 and RC1 as four separate buttons."""

import hashlib
from pathlib import Path

import companion_common
import dk_fs


INPUT_MAP_SOURCE = companion_common.PLUGIN_DIR / "assets" / "ayaneo3-companion.yaml"
INPUT_MAP_TARGET = Path("/etc/inputplumber/capability_maps.d/ayaneo_type7.yaml")
INPUT_MAP_MARKER = b"# Managed by AYANEO 3 Companion\n"
LEGACY_INPUT_MAP_SHA256 = frozenset({
    "a01579e9efc1a9e785412c3c9f6f6882814e97ba112cc76c4c530f37a8b44402",
})
LEGACY_INPUT_DEVICE_TARGET = Path("/etc/inputplumber/devices.d/01-ayaneo3-companion.yaml")
LEGACY_INPUT_MAP_TARGETS = (
    Path("/etc/inputplumber/capability_maps.d/01-ayaneo3-companion-aya7.yaml"),
    Path("/etc/inputplumber/capability_maps.d/ayaneo3-companion.yaml"),
)


def button_map_bytes() -> bytes:
    """Return the aya7 extension with both native and legacy QAM inputs."""
    return INPUT_MAP_SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


def button_map_owned(path: Path) -> bool:
    """Recognise current, marked and known legacy Companion maps."""
    if path in (LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        return path.exists()
    data = companion_common.normalized_file_bytes(path)
    if data is None:
        return False
    return (INPUT_MAP_MARKER in data[:256] or
            hashlib.sha256(data).hexdigest() in LEGACY_INPUT_MAP_SHA256)


def button_fix_installed() -> bool:
    try:
        return INPUT_MAP_TARGET.read_bytes() == button_map_bytes()
    except OSError:
        return False


def install_button_fix() -> None:
    """Extend native aya7 without replacing its APU-specific device profile."""
    companion_common.require_supported_device("key-binding setup")
    if INPUT_MAP_TARGET.is_symlink():
        raise RuntimeError("refusing to replace a symlinked InputPlumber map")
    if INPUT_MAP_TARGET.exists() and not button_map_owned(INPUT_MAP_TARGET):
        raise RuntimeError("another aya7 InputPlumber override already exists")
    INPUT_MAP_TARGET.parent.mkdir(parents=True, exist_ok=True)
    dk_fs.write_atomic(INPUT_MAP_TARGET, button_map_bytes(), 0o644)
    # InputPlumber sorts maps globally by filename and the last duplicate ID
    # wins. Matching the stock ayaneo_type7.yaml filename makes the /etc copy
    # sort after /usr/share via directory priority.
    # Remove the old full-device override. It hard-coded the 8840U USB path and
    # unnecessarily replaced working native mappings on the HX 370 variant.
    for legacy in (LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        if button_map_owned(legacy):
            legacy.unlink()


def remove_button_fix() -> None:
    for target in (INPUT_MAP_TARGET, LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        if button_map_owned(target):
            target.unlink()
