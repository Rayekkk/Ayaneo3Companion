# SPDX-License-Identifier: BSD-3-Clause
"""Offline installer: validate, stage, swap and recover the complete deployment."""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ARCHIVE_ROOT = "Ayaneo3Companion"
MAX_DOWNLOAD = 32 * 1024 * 1024
# The ZIP check is the plugin updater's (dk_update) and the modules it imports.
VALIDATOR = ("dk_update.py", "dk_net.py", "dk_fs.py", "dk_process.py")
# Every file the plugin needs to start, so neither the installer nor the updater installs a broken release.
REQUIRED = (
    "main.py", "plugin.json", "package.json", "dist/index.js", *VALIDATOR, "dk_rpc.py",
    "safe_settings.py", "quality_runtime.py", "lego_updater.py", "fan_backend.py", "fan_sleep.py", "fan_guardian.py",
    "fan_hwmon.py", "fan_policy.py", "active_app.py", "tdp_backend.py", "display_script_io.py", "module_runtime.py",
    "diagnostics.py", "system_process.py", "input_backend.py", "display_backend.py", "audio_backend.py",
    "controller_backend.py", "ec_backend.py", "companion_common.py",
    "assets/ayaneo.ayaneo3.oled.lua", "assets/ayaneo3-companion.yaml",
)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")

LOADER = "plugin_loader.service"
INPUT = "inputplumber.service"
MAP = "capability_maps.d/ayaneo_type7.yaml"
LEGACY_MAPS = (
    "devices.d/01-ayaneo3-companion.yaml",
    "capability_maps.d/01-ayaneo3-companion-aya7.yaml",
    "capability_maps.d/ayaneo3-companion.yaml",
)
LOCK_TIMEOUT_SECONDS = 300
SERVICE_TIMEOUT_SECONDS = 15
SERVICE_STABLE_SECONDS = 2
SERVICE_POLL_SECONDS = 0.1


def resolve_plugin_root(properties):
    """Read Decky's effective systemd configuration, including drop-in overrides."""
    fields = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
    env = dict(item.split("=", 1) for item in shlex.split(fields.get("Environment", "")) if "=" in item)
    # Match Decky's get_privileged_path()/get_unprivileged_path(): plugins live
    # under the privileged base, while data/config/logs may use another base.
    if env.get("PRIVILEGED_PATH"):
        candidate = str(Path(env["PRIVILEGED_PATH"]) / "plugins")
    elif env.get("UNPRIVILEGED_PATH"):
        candidate = str(Path(env["UNPRIVILEGED_PATH"]) / "plugins")
    elif env.get("PLUGIN_PATH"):
        candidate = str(Path(env["PLUGIN_PATH"].rstrip("/")).parent / "plugins")
    else:
        work = Path(fields.get("WorkingDirectory", ""))
        if not work.is_absolute() or work.name != "services":
            raise ValueError("Cannot determine Decky's plugin directory from plugin_loader.service")
        candidate = str(work.parent / "plugins")
    path = Path(candidate)
    if not path.is_absolute() or "$" in candidate or ".." in path.parts:
        raise ValueError("Decky's plugin directory must be an absolute resolved service path")
    path = path.resolve(strict=True)  # /home may legitimately resolve to /var/home.
    if not path.is_dir() or path == Path(path.anchor):
        raise ValueError("Decky's plugin directory is missing or invalid")
    return path


class SystemServices:
    def run(self, action, service):
        subprocess.run(["systemctl", action, service], check=True, timeout=45)

    def is_active(self, service):
        result = subprocess.run(["systemctl", "is-active", service], capture_output=True,
                                text=True, timeout=10)
        if result.returncode not in (0, 3):
            raise RuntimeError(f"Cannot read {service} state: {result.stderr.strip()}")
        return result.returncode == 0

    def status(self, service):
        result = subprocess.run(
            ["systemctl", "show", service, "--property=ActiveState", "--property=SubState",
             "--property=MainPID", "--property=NRestarts", "--property=Result"],
            check=True, capture_output=True, text=True, timeout=5)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        try:
            return {"active": fields["ActiveState"], "sub": fields["SubState"],
                    "pid": int(fields["MainPID"]), "restarts": int(fields["NRestarts"]),
                    "result": fields["Result"]}
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"Cannot verify {service} state") from error


def _wait_for_service(services, service, active):
    """Require a stopped process, or a stable running process after a start ack."""
    _wait_for_services(services, ((service, active),))


def _wait_for_services(services, expectations):
    """Observe all required services in the same window before discarding backup."""
    deadline = time.monotonic() + SERVICE_TIMEOUT_SECONDS
    identity = None
    stable_since = None
    while True:
        now = time.monotonic()
        statuses = {name: services.status(name) for name, _ in expectations}
        healthy = all(
            (status["active"] == "active" and status["sub"] == "running"
             and status["pid"] > 0 and status["result"] == "success") if active else
            (status["active"] in ("inactive", "failed") and status["pid"] == 0)
            for name, active in expectations for status in (statuses[name],))
        if healthy:
            if not any(active for _, active in expectations):
                return
            current = tuple((name, statuses[name]["pid"], statuses[name]["restarts"])
                            for name, _ in expectations)
            if current != identity:
                identity, stable_since = current, now
            if now - stable_since >= SERVICE_STABLE_SECONDS:
                return
        else:
            identity = stable_since = None
        if time.monotonic() >= deadline:
            wanted = "stably running" if any(active for _, active in expectations) else "fully stopped"
            names = ", ".join(name for name, _ in expectations)
            raise TimeoutError(f"{names} did not become {wanted}: {statuses}")
        time.sleep(min(SERVICE_POLL_SECONDS, max(0, deadline - time.monotonic())))


def _change_service(services, action, service):
    services.run(action, service)
    _wait_for_service(services, service, action != "stop")


@contextmanager
def _deployment_lock(root, timeout=LOCK_TIMEOUT_SECONDS):
    """Serialize processes without a removable lock file or shared temp path."""
    if os.name == "posix":
        import fcntl
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Another AYANEO 3 Companion installation is running")
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            yield
        finally:
            os.close(fd)
    elif os.name == "nt":
        # The Linux installer uses flock; a named mutex lets Windows exercise
        # the same cross-process exclusion without modifying the payload tree.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        key = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, "Local\\Ayaneo3CompanionInstaller." + key)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, min(int(timeout * 1000), 0xFFFFFFFE))
            if result == 0x102:
                raise TimeoutError("Another AYANEO 3 Companion installation is running")
            if result not in (0, 0x80):
                raise ctypes.WinError(ctypes.get_last_error())
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        raise RuntimeError("Safe deployment locking is unavailable on this platform")


def _validator(archive):
    """dk_update, from beside this file or else from the same ZIP: a bootstrap.sh of 1.2/1.3 extracts only this
    installer and lego_updater.py into its private folder."""
    here = Path(__file__).resolve().parent
    if not all((here / name).is_file() for name in VALIDATOR):
        with zipfile.ZipFile(archive) as payload:
            for name in VALIDATOR:
                entry = payload.getinfo(f"{ARCHIVE_ROOT}/{name}")
                if entry.is_dir() or not 0 < entry.file_size <= 1024 * 1024:
                    raise ValueError(f"Invalid installer file: {name}")
                (here / name).write_bytes(payload.read(entry))
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    import dk_update
    return dk_update


def _check(archive, data, version):
    """ValueError unless data (the bytes about to be installed) is a complete Companion package at version."""
    dk_update = _validator(archive)
    updates = dk_update.Updates(repo="Rayekkk/Ayaneo3Companion", folder=ARCHIVE_ROOT,
                                plugin_name="AYANEO 3 Companion", package_name="ayaneo3companion",
                                flags=["root"], current="0.0.0", home="/nonexistent", required=REQUIRED)
    try:
        updates.validate(data, version)
    except dk_update.UpdateError as error:
        raise ValueError(str(error)) from error


def validate_archive(archive):
    if not 0 < Path(archive).stat().st_size <= MAX_DOWNLOAD:
        raise ValueError("The plugin ZIP exceeds the download size limit")
    data = Path(archive).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as payload:
        if payload.getinfo(f"{ARCHIVE_ROOT}/plugin.json").file_size > 65536:
            raise ValueError("The release manifest is too large")
        manifest = json.loads(payload.read(f"{ARCHIVE_ROOT}/plugin.json"))
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("The release version must use stable X.Y.Z numbering.")
    _check(archive, data, version)
    return version


def _owned_map(path):
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return (b"# Managed by AYANEO 3 Companion" in data[:256]
            or b"name: AYANEO 3 Companion\n" in data)


def _atomic_copy(source, target):
    # Use the target filesystem: /home and /etc can be different mounts.
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".ayaneo3-map-", dir=target.parent,
                                     delete=False) as stream:
        pending = Path(stream.name)
    try:
        shutil.copy2(source, pending)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            info = source.stat()
            os.chown(pending, info.st_uid, info.st_gid)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def _remove_transaction(root, transaction):
    resolved = transaction.resolve(strict=True)
    if (resolved != transaction or resolved.parent != root
            or not resolved.name.startswith(".ayaneo3-install-")):
        raise RuntimeError("Refusing to remove an unexpected installer staging directory")
    shutil.rmtree(resolved)


def install_archive(archive, plugin_root, input_root, services):
    version = validate_archive(archive)
    root = Path(plugin_root).resolve(strict=True)
    if not root.is_dir() or root == Path(root.anchor):
        raise ValueError("Invalid plugin root")
    with _deployment_lock(root):
        return _install_locked(archive, version, root, input_root, services)


def _install_locked(archive, version, root, input_root, services):
    target = root / ARCHIVE_ROOT
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("The installed plugin is not a regular directory")
    map_target = Path(input_root) / MAP
    legacy = [Path(input_root) / name for name in LEGACY_MAPS]
    for path in (map_target, *legacy):
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"Refusing to replace a non-regular InputPlumber map: {path}")
    if map_target.exists() and not _owned_map(map_target):
        raise ValueError(f"Another aya7 InputPlumber override already exists: {map_target}")
    maps = [map_target, *(path for path in legacy if path.exists() and _owned_map(path))]
    was_active = {name: services.is_active(name) for name in (LOADER, INPUT)}
    transaction = Path(tempfile.mkdtemp(prefix=".ayaneo3-install-", dir=root)).resolve()
    stage, previous = transaction / ARCHIVE_ROOT, transaction / "previous"
    saved_maps = {}
    stopped = old_moved = new_moved = maps_changed = False
    recoverable = True
    try:
        # Validate/extract from the same file before stopping any service.
        data = Path(archive).read_bytes()  # checked and extracted from the same bytes
        _check(archive, data, version)
        with zipfile.ZipFile(io.BytesIO(data)) as payload:
            payload.extractall(transaction)
        for path in stage.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        stage.chmod(0o755)
        for index, path in enumerate(maps):
            backup = transaction / f"map-{index}.bak"
            if path.exists():
                shutil.copy2(path, backup)
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    info = path.stat()
                    os.chown(backup, info.st_uid, info.st_gid)
                saved_maps[path] = backup
            else:
                saved_maps[path] = None
        stopped = True  # A failed stop may already have stopped the service.
        _change_service(services, "stop", LOADER)
        if target.exists():
            os.replace(target, previous)
            old_moved = True
        os.replace(stage, target)
        new_moved = True
        maps_changed = True
        _atomic_copy(target / "assets/ayaneo3-companion.yaml", map_target)
        for path in maps[1:]:
            path.unlink()
        _change_service(services, "restart", INPUT)
        _change_service(services, "start", LOADER)
        _wait_for_services(services, ((INPUT, True), (LOADER, True)))
    except BaseException as error:
        failures = []
        if stopped:
            try:
                _change_service(services, "stop", LOADER)
            except Exception as recovery:
                failures.append(f"stop loader: {recovery}")
            # Preserve the running deployment if it cannot be stopped. Moving
            # its files would mix a live new process with the old deployment.
            if not failures:
                try:
                    if new_moved:
                        os.replace(target, stage)
                    if old_moved:
                        os.replace(previous, target)
                except Exception as recovery:
                    failures.append(f"plugin files: {recovery}")
            if not failures and maps_changed:
                for path, backup in saved_maps.items():
                    try:
                        if backup is None:
                            path.unlink(missing_ok=True)
                        else:
                            _atomic_copy(backup, path)
                    except Exception as recovery:
                        failures.append(f"{path}: {recovery}")
            # Never restart Decky with a deployment whose rollback is incomplete.
            if not failures:
                for name in (INPUT, LOADER):
                    try:
                        _change_service(services, ("restart" if name == INPUT else "start")
                                        if was_active[name] else "stop", name)
                    except Exception as recovery:
                        failures.append(f"{name}: {recovery}")
                        break
                if not failures:
                    try:
                        _wait_for_services(services, tuple((name, was_active[name])
                                                          for name in (INPUT, LOADER)))
                    except Exception as recovery:
                        failures.append(f"restored services: {recovery}")
        if failures:
            recoverable = False
            raise RuntimeError(f"{error}; rollback failed: {'; '.join(failures)}; "
                               f"recovery files retained at {transaction}") from error
        raise RuntimeError(f"{error}; previous deployment restored") from error
    finally:
        if recoverable:
            _remove_transaction(root, transaction)
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    version = validate_archive(args.archive)
    properties = subprocess.run(["systemctl", "show", LOADER, "--property=Environment",
                                 "--property=WorkingDirectory"], check=True, capture_output=True,
                                text=True, timeout=10).stdout
    root = resolve_plugin_root(properties)
    print(f"AYANEO 3 Companion {version} -> {root / ARCHIVE_ROOT}", flush=True)
    if not args.check:
        if os.geteuid() != 0:
            raise SystemExit("Run installation through bootstrap.sh (root privileges required)")
        if (Path("/sys/class/dmi/id/sys_vendor").read_text().strip() != "AYANEO"
                or Path("/sys/class/dmi/id/product_name").read_text().strip() != "AYANEO 3"):
            raise SystemExit("AYANEO 3 is required")
        install_archive(args.archive, root, Path("/etc/inputplumber"), SystemServices())
        print("Installed. Return to Game Mode and enable Fix Key Binding if needed.")


if __name__ == "__main__":
    main()
