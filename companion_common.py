# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Shared Companion runtime: device identity, plugin paths and privileged I/O."""

import json
import os
from pathlib import Path

import decky

import dk_fs

LOG = "[ayaneo3companion]"
PLUGIN_DIR = Path(decky.DECKY_PLUGIN_DIR)

def decky_account():
    try:
        import pwd
        name = getattr(decky, "DECKY_USER", None) or os.environ.get("DECKY_USER")
        account = pwd.getpwnam(name) if name else None
        if account is not None and account.pw_uid > 0:
            return dict(user_home=os.path.realpath(account.pw_dir), user_uid=account.pw_uid,
                        user_gid=account.pw_gid)
    except (ImportError, KeyError):
        pass
    return {}


DECKY_ACCOUNT = decky_account()
# GitHub release downloads (RyzenAdj): the release URL may only lead to GitHub's asset storage.
GITHUB_ASSET_HOSTS = frozenset({"objects.githubusercontent.com", "release-assets.githubusercontent.com"})
GITHUB_RELEASE_HOSTS = GITHUB_ASSET_HOSTS | {"github.com"}
HARDWARE_DOWNLOAD_SECONDS = 60


def plugin_version() -> str:
    """The installed version: Decky's DECKY_PLUGIN_VERSION (from package.json), else package.json itself."""
    version = os.environ.get("DECKY_PLUGIN_VERSION", "")
    if not version:
        try:
            version = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8")).get("version", "")
        except (OSError, ValueError, AttributeError):
            version = ""
    return version if isinstance(version, str) and version else "0.0.0"


# SteamOS keeps /var per OS slot (var-A/var-B partitions), so an update that switches
# slots leaves it behind; /home is shared. Measured on the console.
BACKUP_ROOT = Path("/home/.ayaneo3companion-backups")
LEGACY_BACKUP_ROOT = Path("/var/lib/ayaneo3-companion")
LEGACY_BACKUP_DIRS = ("audio-calibration", "display-definition", "backups")


def private_dir(path: Path) -> Path:
    """A root-only folder (0700), never through a link."""
    for folder in (path.parent, path):
        if folder.is_symlink():
            raise RuntimeError(f"refusing a symlinked backup folder: {folder}")
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
    return path


def migrate_backups(source_root: Path = LEGACY_BACKUP_ROOT, target_root: Path = BACKUP_ROOT) -> int:
    """Copy backups older releases kept in /var/lib (one OS slot) to target_root; originals stay."""
    copied = 0
    for name in LEGACY_BACKUP_DIRS:
        source = source_root / name
        if source.is_symlink() or not source.is_dir():
            continue
        for item in sorted(source.iterdir()):
            target = target_root / name / item.name
            if item.is_symlink() or not item.is_file() or target.exists():
                continue
            data = item.read_bytes()
            private_dir(target.parent)
            # Atomic: a copy cut short would otherwise stay truncated, skipped as existing on every later start.
            dk_fs.write_atomic(target, data, 0o600, no_links=True)
            if target.read_bytes() != data:
                raise RuntimeError(f"backup copy verification failed: {target}")
            copied += 1
    return copied


_dmi_cache: dict[str, str] = {}


def dmi(name: str) -> str:
    # Firmware identity never changes while the system runs; it is asked before every hardware write.
    if name in _dmi_cache:
        return _dmi_cache[name]
    try:
        value = Path(f"/sys/class/dmi/id/{name}").read_text().strip()
    except OSError:
        return ""
    if value:
        _dmi_cache[name] = value
    return value


def supported_device() -> bool:
    return dmi("sys_vendor").upper() == "AYANEO" and dmi("product_name").upper() == "AYANEO 3"


def require_supported_device(action: str = "hardware control") -> None:
    """Enforce the AYANEO 3 boundary in the privileged backend."""
    if not supported_device():
        raise RuntimeError(f"{action} is restricted to AYANEO 3")


def clamp(value, low, high):
    return max(low, min(high, int(value)))


def normalized_file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return None
