# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""Shared Companion runtime: device identity, plugin paths, privileged I/O and downloads."""

import contextlib
import os
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

import decky

# Decky aliases its own updater module to the bare name ``updater`` before a
# plugin is loaded, so use the collision-proof module name shared by the other
# Rayek plugins.
from lego_updater import Updater

LOG = "[ayaneo3companion]"
PLUGIN_DIR = Path(decky.DECKY_PLUGIN_DIR)

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/Rayekkk/Ayaneo3Companion/releases/latest"
)


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
updater = Updater(
    releases_url=GITHUB_RELEASES_URL,
    user_agent="Ayaneo3Companion",
    log_prefix=LOG,
    plugin_dir=str(PLUGIN_DIR),
    asset_name_template="Ayaneo3Companion-{version}.zip",
    logger=decky.logger,
    **DECKY_ACCOUNT,
)
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com", "raw.githubusercontent.com",
})
HARDWARE_DOWNLOAD_SECONDS = 60.0
HARDWARE_NETWORK_TIMEOUT = 10.0
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
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(target, flags, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
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


def atomic_write_bytes(target: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace a regular configuration file without following a shared temp name."""
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                        dir=str(target.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def clamp(value, low, high):
    return max(low, min(high, int(value)))


def checked_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise RuntimeError(f"refusing untrusted hardware download host: {parsed.hostname or '?'}")
    return url


class _HardwareDownloadRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Validate before following a redirect; checking only the final URL
        # would still send a privileged request to an untrusted destination.
        checked_download_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def open_hardware_download(request):
    opener = urllib.request.build_opener(
        _HardwareDownloadRedirect(), urllib.request.HTTPSHandler(context=updater.ssl_context()))
    return opener.open(request, timeout=HARDWARE_NETWORK_TIMEOUT)


def hardware_download_chunks(response, limit: int, deadline: float):
    """Bound transfer duration as well as bytes, even when the peer drips data."""
    reader = getattr(response, "read1", response.read)
    total = 0
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("hardware download exceeded its time limit")
        chunk = reader(min(65536, limit + 1 - total))
        if time.monotonic() >= deadline:
            raise TimeoutError("hardware download exceeded its time limit")
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            raise RuntimeError("hardware download exceeded the size limit")
        yield chunk


def normalized_file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return None
