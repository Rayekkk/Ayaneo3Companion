# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 power limits: TDP through RyzenAdj, PowerStation ownership, CPU Boost and GPU power."""

import glob
import hashlib
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import companion_common
import system_process


BIN_DIR = companion_common.PLUGIN_DIR / "bin"
RYZENADJ = BIN_DIR / "ryzenadj"
RYZENADJ_LIB = BIN_DIR / "libryzenadj.so"
RYZENADJ_URL = "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/ryzenadj-manylinux_2_28-x86_64.tar.gz"
RYZENADJ_ARCHIVE_SHA256 = "d04547f111c6af3e40d3f210468adb884561618ddade0b640d90e50c88d03444"
RYZENADJ_BINARY_SHA256 = "18a61170efec95d2366355b9dd5c75a961a9e8008d42e3471f4f414a6faec471"
RYZENADJ_LIBRARY_SHA256 = "665a91ee17273e0a8039eacfc91d2753e87148ea9983feb683a54f3161005f87"
CPU_BOOST_PATH = Path("/sys/devices/system/cpu/cpufreq/boost")
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
DEFAULT_TDP = {"spl": 15, "sppt": 18, "fppt": 25}
PRESETS = {
    "Minimum": {"spl": 5, "sppt": 8, "fppt": 10},
    "Low power": {"spl": 8, "sppt": 10, "fppt": 12},
    "Balanced": {"spl": 15, "sppt": 18, "fppt": 25},
    "Performance": {"spl": 30, "sppt": 32, "fppt": 35},
    "Max": {"spl": 32, "sppt": 35, "fppt": 37},
}
_tdp_apply_lock = threading.Lock()
_cpu_boost_lock = threading.Lock()


def normalize_tdp(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    spl = companion_common.clamp(source.get("spl", DEFAULT_TDP["spl"]), 5, 35)
    sppt = companion_common.clamp(source.get("sppt", DEFAULT_TDP["sppt"]), spl, 37)
    fppt = companion_common.clamp(source.get("fppt", DEFAULT_TDP["fppt"]), sppt, 37)
    return {"spl": spl, "sppt": sppt, "fppt": fppt}


def tdp_preset(raw, stored=None) -> str:
    """Keep the explicit UI choice, falling back to value-based migration."""
    if stored in (*PRESETS.keys(), "Custom"):
        return stored
    value = normalize_tdp(raw)
    for name, limits in PRESETS.items():
        if value == limits:
            return name
    return "Custom"


def _powerstation_active():
    try:
        return _probe_powerstation_active()
    except (OSError, subprocess.SubprocessError) as error:
        # Ownership failed before any hardware write. Mark the capability
        # unavailable and avoid a rollback that only repeats the same stalled
        # service probe while reserving the public settings queue.
        raise TdpUnavailableError(
            f"Could not verify whether PowerStation owns TDP control: {error}") from error


def _probe_powerstation_active():
    if system_process.run(["busctl", "--system", "status", "org.shadowblip.PowerStation"], 5,
                          capture_output=True).returncode == 0:
        return True
    # Also detect startup/reload periods before D-Bus owns its name. A missing
    # GPU TDP property must never cause a fallback to a competing RyzenAdj write.
    result = system_process.run(["systemctl", "show", "powerstation.service",
                                 "--property=ActiveState", "--value"], 5,
                                capture_output=True, text=True)
    if result.returncode not in (0, 4):
        raise TdpUnavailableError("Could not verify whether PowerStation owns TDP control")
    if result.returncode == 4:
        return False
    state = result.stdout.strip()
    if state in ("inactive", "failed"):
        return False
    if state in ("active", "activating", "reloading", "deactivating", "refreshing", "maintenance"):
        return True
    raise TdpUnavailableError("Could not verify whether PowerStation owns TDP control")


def _download_archive(target: Path) -> None:
    deadline = time.monotonic() + companion_common.HARDWARE_DOWNLOAD_SECONDS
    request = urllib.request.Request(
        companion_common.checked_download_url(RYZENADJ_URL),
        headers={"User-Agent": f"Ayaneo3Companion/{companion_common.updater.plugin_version()}"},
    )
    with companion_common.open_hardware_download(request) as response:
        companion_common.checked_download_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("RyzenAdj archive is unexpectedly large")
        with target.open("wb") as output:
            for chunk in companion_common.hardware_download_chunks(response, MAX_DOWNLOAD_BYTES, deadline):
                output.write(chunk)


def _ensure_ryzenadj() -> None:
    expected = (("ryzenadj", RYZENADJ, RYZENADJ_BINARY_SHA256, 0o755),
                ("libryzenadj.so", RYZENADJ_LIB, RYZENADJ_LIBRARY_SHA256, 0o644))
    def valid_file(target: Path, digest: str) -> bool:
        try:
            if target.is_symlink() or not target.is_file() or target.stat().st_size > MAX_DOWNLOAD_BYTES:
                return False
            return hashlib.sha256(target.read_bytes()).hexdigest() == digest
        except OSError:
            return False

    if all(valid_file(target, digest) for _name, target, digest, _mode in expected):
        RYZENADJ.chmod(0o755)
        return
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "ryzenadj.tar.gz"
        _download_archive(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != RYZENADJ_ARCHIVE_SHA256:
            raise RuntimeError("RyzenAdj archive checksum mismatch")
        verified = []
        with tarfile.open(archive, "r:gz") as bundle:
            for name, target, digest, mode in expected:
                members = [item for item in bundle.getmembers() if Path(item.name).name == name]
                if len(members) != 1 or not members[0].isfile():
                    raise RuntimeError(f"RyzenAdj archive is missing {name}")
                member = members[0]
                if not 0 < member.size <= MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"RyzenAdj archive has an invalid {name} size")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"could not extract {name}")
                with source:
                    data = source.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) != member.size or hashlib.sha256(data).hexdigest() != digest:
                    raise RuntimeError(f"RyzenAdj {name} checksum mismatch")
                verified.append((target, data, mode))
        # Never replace an installed component with unverified or partial data.
        # The library is published first so the executable is never considered
        # ready while its required library is still missing.
        for target, data, mode in reversed(verified):
            companion_common.atomic_write_bytes(target, data, mode)


def tdp_backend() -> str:
    return "PowerStation" if _powerstation_active() else "RyzenAdj"


def read_cpu_boost() -> bool:
    """Read the kernel-wide CPU frequency boost permission."""
    try:
        value = CPU_BOOST_PATH.read_text().strip()
    except OSError as error:
        raise RuntimeError("CPU Boost is unavailable on this kernel") from error
    if value not in ("0", "1"):
        raise RuntimeError(f"unexpected CPU Boost value: {value or 'empty'}")
    return value == "1"


def write_cpu_boost(enabled: bool) -> None:
    """Set CPU Boost through CPUFreq's global sysfs control and verify it."""
    companion_common.require_supported_device("CPU Boost")
    expected = bool(enabled)
    with _cpu_boost_lock:
        previous = read_cpu_boost()
        try:
            CPU_BOOST_PATH.write_text("1" if expected else "0")
            if read_cpu_boost() != expected:
                raise RuntimeError("kernel did not keep the requested CPU Boost state")
        except Exception as error:
            try:
                CPU_BOOST_PATH.write_text("1" if previous else "0")
                if read_cpu_boost() != previous:
                    raise RuntimeError("CPU Boost rollback readback mismatch")
            except Exception as recovery:
                raise RuntimeError(f"{error}; CPU Boost recovery failed: {recovery}") from error
            raise RuntimeError(f"could not change CPU Boost: {error}") from error


POWERSTATION_TDP_ERROR = (
    "TDP control is unavailable while PowerStation is active: its TDP/Boost API "
    "cannot verify SPL, SPPT and FPPT or enforce the 37 W fast-limit ceiling."
)


class TdpUnavailableError(RuntimeError):
    """The available writer cannot safely implement three independent limits."""


def tdp_capabilities():
    try:
        backend = tdp_backend()
        return {"tdp_backend": backend, "tdp_supported": backend != "PowerStation",
                "tdp_error": POWERSTATION_TDP_ERROR if backend == "PowerStation" else ""}
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        return {"tdp_backend": "Unavailable", "tdp_supported": False, "tdp_error": str(error)}


def _apply_tdp_unlocked(config: dict) -> None:
    companion_common.require_supported_device("TDP control")
    values = normalize_tdp(config)
    if _powerstation_active():
        # Neither the RyzenAdj nor hwmon PowerStation adapter exposes a verified
        # three-limit contract. Do not guess FPPT or bypass its active writer.
        raise TdpUnavailableError(POWERSTATION_TDP_ERROR)
    _ensure_ryzenadj()
    # Initial verified downloads can take time; do not rely on the pre-download
    # ownership check if the system started another writer in the meantime.
    if _powerstation_active():
        raise TdpUnavailableError(POWERSTATION_TDP_ERROR)
    env = system_process.system_env(LC_ALL="C", LD_LIBRARY_PATH=str(BIN_DIR))
    result = system_process.run([str(RYZENADJ), f"--stapm-limit={values['spl'] * 1000}",
                                 f"--slow-limit={values['sppt'] * 1000}",
                                 f"--fast-limit={values['fppt'] * 1000}"],
                                10, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "RyzenAdj failed")


def apply_tdp(config: dict) -> None:
    # RPC, game changes, startup restore and charger restore can all arrive on
    # separate worker threads. Never interleave two three-register writes.
    with _tdp_apply_lock:
        _apply_tdp_unlocked(config)


def gpu_power_watts():
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if Path(hwmon, "name").read_text().strip() == "amdgpu":
                return round(int(Path(hwmon, "power1_average").read_text()) / 1_000_000, 1)
        except (OSError, ValueError):
            pass
    return None
