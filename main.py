# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/Ayaneo3Companion

"""AYANEO 3 hardware controls for Decky Loader."""

import asyncio
import colorsys
import contextlib
import glob
import hashlib
import json
import math
import os
import select
import shutil
import ssl
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path

import decky
from settings import SettingsManager

LOG = "[ayaneo3companion]"
PLUGIN_DIR = Path(decky.DECKY_PLUGIN_DIR)
BIN_DIR = PLUGIN_DIR / "bin"
RYZENADJ = BIN_DIR / "ryzenadj"
RYZENADJ_LIB = BIN_DIR / "libryzenadj.so"
RYZENADJ_URL = "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/ryzenadj-manylinux_2_28-x86_64.tar.gz"
RYZENADJ_ARCHIVE_SHA256 = "d04547f111c6af3e40d3f210468adb884561618ddade0b640d90e50c88d03444"
RYZENADJ_BINARY_SHA256 = "18a61170efec95d2366355b9dd5c75a961a9e8008d42e3471f4f414a6faec471"
ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "github.com", "release-assets.githubusercontent.com", "raw.githubusercontent.com",
})
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)
_ssl_ctx = None

LUA_SOURCE = PLUGIN_DIR / "assets" / "ayaneo.ayaneo3.oled.lua"
LUA_TARGET = Path("/etc/gamescope/scripts/00-gamescope/displays/ayaneo.ayaneo3.oled.lua")
PUBLISHED_EDID = Path("/home/deck/.config/gamescope/edid.bin")
EDID_TARGET_NITS = 800
INPUT_MAP_SOURCE = PLUGIN_DIR / "assets" / "ayaneo3-companion.yaml"
INPUT_MAP_TARGET = Path("/etc/inputplumber/capability_maps.d/ayaneo_type7.yaml")
LEGACY_INPUT_DEVICE_TARGET = Path("/etc/inputplumber/devices.d/01-ayaneo3-companion.yaml")
LEGACY_INPUT_MAP_TARGETS = (
    Path("/etc/inputplumber/capability_maps.d/01-ayaneo3-companion-aya7.yaml"),
    Path("/etc/inputplumber/capability_maps.d/ayaneo3-companion.yaml"),
)
AUDIO_FIRMWARE_PATH = Path("/sys/module/firmware_class/parameters/path")
AUDIO_FIRMWARE_ROOT = Path("/var/lib/ayaneo3-companion/firmware")
AUDIO_FIRMWARE_SYSTEM_DIR = Path("/usr/lib/firmware/cirrus")
AUDIO_FIRMWARE_STEM = "cs35l41-dsp1-spk-prot-1f660105"
AUDIO_FIRMWARE_WMFW_STEM = "cs35l41-dsp1-spk-prot-1f660105"
AUDIO_FIRMWARE_ALIASES = (
    f"{AUDIO_FIRMWARE_STEM}-spkid1-l0",
    f"{AUDIO_FIRMWARE_STEM}-spkid1-r0",
)
AUDIO_FIRMWARE_CONTROLS = (
    "L0 DSP1 Firmware Load",
    "R0 DSP1 Firmware Load",
)
AUDIO_FIRMWARE_TYPE_CONTROLS = (
    "L0 DSP1 Firmware Type",
    "R0 DSP1 Firmware Type",
)
AUDIO_CALIBRATION_URL = (
    "https://raw.githubusercontent.com/hhd-dev/hwfirm/master/cirrus/"
    "cs35l41-dsp1-spk-cali-1f660105-spkid1.bin"
)
AUDIO_CALIBRATION_SHA256 = "966262929355aeaf01e0a4c193d3be1e23443a59438b71e3d8daefe2fc6d4f59"
AUDIO_CALIBRATION_BIN = "cs35l41-dsp1-spk-cali-1f660105-spkid1.bin"
AUDIO_CALIBRATION_WMFW = "cs35l41-dsp1-spk-cali-1f660105-spkid1"
AUDIO_CALIBRATION_AMBIENT = 23
AUDIO_CALIBRATION_BACKUP_ROOT = Path("/var/lib/ayaneo3-companion/audio-calibration")
AUDIO_CALIBRATION_EFI_GLOB = (
    "/sys/firmware/efi/efivars/"
    "CirrusSmartAmpCalibrationData-02f9af02-7734-4233-b43d-93fe5aa35db3"
)
AUDIO_EFI_HEADER = struct.Struct("<II")
AUDIO_EFI_RECORD = struct.Struct("<QQbBH")
AUDIO_EFI_ATTRIBUTES = 0x00000007
AUDIO_EFI_PAYLOAD_SIZE = AUDIO_EFI_HEADER.size + 2 * AUDIO_EFI_RECORD.size
AUDIO_AMPLIFIERS = (("left", "0x40"), ("right", "0x41"))
AUDIO_DSP_REGISTERS = {
    "cal_r": 0x02800268,
    "ambient": 0x0280026C,
    "status": 0x02800270,
    "checksum": 0x02800274,
}
AUDIO_USER_STOP_UNITS = (
    "pipewire-pulse.socket", "pipewire-pulse.service", "wireplumber.service",
    "pipewire.socket", "pipewire.service",
)
AUDIO_USER_START_UNITS = (
    "pipewire.socket", "pipewire.service", "wireplumber.service",
    "pipewire-pulse.socket", "pipewire-pulse.service",
)
AUDIO_LEGACY_TEST_PATHS = frozenset({
    "/home/deck/ayaneo3-audio-cal-test",
    "/home/deck/ayaneo3-audio-fix",
})
POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")
EC_CHARGE_REGISTER = 0x1E
EC_CHARGE_AUTO = 0xAA
EC_CHARGE_INHIBIT = 0x55
EC_CONTROLLER_POWER_REGISTER = 0x2D
EC_CONTROLLER_POWER_OFF = 0xFE
EC_CONTROLLER_POWER_ON = 0xFF
EC_MODULE_REGISTER = 0x2F
EC_MODULE_LEFT = 0x01
EC_MODULE_RIGHT = 0x02
EC_MODULE_MASK = EC_MODULE_LEFT | EC_MODULE_RIGHT

# AYANEO reports a separate identifier for each physical component layout. Bit
# 6 selects the reversed arrangement, but the UI reports the useful upper/lower
# positions instead of calling a correctly installed module "rotated".
LEFT_MODULES = {
    0x02: "Cross Film / Joystick",
    0x04: "Cross / Joystick",
    0x06: "Cross / Touchpad",
    0x08: "Direction / Joystick",
    0x42: "Joystick / Cross Film",
    0x44: "Joystick / Cross",
    0x46: "Touchpad / Cross",
    0x48: "Joystick / Direction",
}
RIGHT_MODULES = {
    0x10: "ABXY / Joystick",
    0x12: "ABXY / Touchpad",
    0x14: "ABXYCZ Fighting",
    0x16: "ABXY Film / Joystick",
    0x50: "Joystick / ABXY",
    0x52: "Touchpad / ABXY",
    0x54: "ABXYCZ Fighting [R]",
    0x56: "Joystick / ABXY Film",
}
MODULE_LAYOUTS = {
    "left": {
        0x02: "Top: Cross Film · Bottom: Joystick",
        0x04: "Top: Cross · Bottom: Joystick",
        0x06: "Top: Cross · Bottom: Touchpad",
        0x08: "Top: Direction · Bottom: Joystick",
        0x42: "Top: Joystick · Bottom: Cross Film",
        0x44: "Top: Joystick · Bottom: Cross",
        0x46: "Top: Touchpad · Bottom: Cross",
        0x48: "Top: Joystick · Bottom: Direction",
    },
    "right": {
        0x10: "Top: Joystick · Bottom: ABXY",
        0x12: "Top: Touchpad · Bottom: ABXY",
        0x14: "Six-button fighting layout",
        0x16: "Top: Joystick · Bottom: ABXY Film",
        0x50: "Top: ABXY · Bottom: Joystick",
        0x52: "Top: ABXY · Bottom: Touchpad",
        0x54: "Six-button fighting layout · R variant",
        0x56: "Top: ABXY Film · Bottom: Joystick",
    },
}
MODULE_INFO_LEFT_INDEX = 32
MODULE_INFO_RIGHT_INDEX = 33
TM_GUARD_INTERVAL = 0.5

VIBRATION_VALUES = {"off": 0x04, "low": 0x01, "medium": 0x02, "high": 0x03}
RGB_MODES = {"off": 0xFF, "solid": 0x01, "pulse": 0x02, "rainbow": 0x03}
EVIOCGBIT_FF = 0x80204535
EVIOCSFF = 0x40304580
EVIOCRMFF = 0x40044581
EV_FF = 0x15
FF_RUMBLE = 0x50
FF_GAIN = 0x60
VIBRATION_CONFIRM_MS = 500
VIBRATION_TEST_MS = 500
DEFAULT_CONTROLLER = {
    "vibration": "high", "ff_gain": 100,
    "rgb_mode": "solid", "color": "6600ff", "brightness": 100,
}
DEFAULT_TDP = {"spl": 15, "sppt": 18, "fppt": 25}
PRESETS = {
    "Minimum": {"spl": 5, "sppt": 8, "fppt": 10},
    "Low power": {"spl": 8, "sppt": 10, "fppt": 12},
    "Balanced": {"spl": 15, "sppt": 18, "fppt": 25},
    "Performance": {"spl": 30, "sppt": 32, "fppt": 35},
    "Max": {"spl": 32, "sppt": 35, "fppt": 37},
}

settings = SettingsManager(name="settings", settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR)
_lock = threading.RLock()
_tdp_apply_lock = threading.Lock()
_ec_lock = threading.Lock()
_controller_apply_lock = threading.Lock()
_audio_apply_lock = threading.Lock()

def _dmi(name: str) -> str:
    try:
        return Path(f"/sys/class/dmi/id/{name}").read_text().strip()
    except OSError:
        return ""


def supported_device() -> bool:
    return _dmi("sys_vendor").upper() == "AYANEO" and _dmi("product_name").upper() == "AYANEO 3"


def _audio_firmware_source() -> Path | None:
    for suffix in (".bin.zst", ".bin.xz", ".bin"):
        source = AUDIO_FIRMWARE_SYSTEM_DIR / f"{AUDIO_FIRMWARE_STEM}{suffix}"
        if source.is_file():
            return source
    return None


def _audio_wmfw_source() -> Path | None:
    for suffix in (".wmfw.zst", ".wmfw.xz", ".wmfw"):
        source = AUDIO_FIRMWARE_SYSTEM_DIR / f"{AUDIO_FIRMWARE_WMFW_STEM}{suffix}"
        if source.is_file():
            return source
    return None


def _audio_alias_filenames(source: Path) -> tuple[str, str]:
    if not source.name.startswith(AUDIO_FIRMWARE_STEM):
        raise RuntimeError("unexpected AYANEO audio firmware filename")
    suffix = source.name[len(AUDIO_FIRMWARE_STEM):]
    if suffix not in (".bin.zst", ".bin.xz", ".bin"):
        raise RuntimeError("unsupported AYANEO audio firmware compression")
    return tuple(f"{alias}{suffix}" for alias in AUDIO_FIRMWARE_ALIASES)


def _same_file(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        return hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    except OSError:
        return False


def _clean_subprocess_env() -> dict:
    environment = os.environ.copy()
    # Decky's PyInstaller runtime ships private libraries that are incompatible
    # with SteamOS systemctl and some host multimedia utilities.
    environment.pop("LD_LIBRARY_PATH", None)
    environment.pop("LD_PRELOAD", None)
    return environment


def _prepare_audio_aliases() -> tuple[Path, Path]:
    source = _audio_firmware_source()
    if source is None:
        raise RuntimeError("SteamOS AYANEO 3 audio firmware is missing")
    target_dir = AUDIO_FIRMWARE_ROOT / "cirrus"
    target_dir.mkdir(parents=True, exist_ok=True)
    targets = tuple(target_dir / name for name in _audio_alias_filenames(source))
    for target in targets:
        if _same_file(source, target):
            continue
        temporary = target.with_name(f".{target.name}.tmp")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    return targets


def _audio_card_index() -> int | None:
    for card in range(8):
        try:
            result = subprocess.run(["amixer", "-c", str(card), "controls"],
                                    capture_output=True, text=True, timeout=5,
                                    env=_clean_subprocess_env())
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0 and all(name in result.stdout for name in AUDIO_FIRMWARE_CONTROLS):
            return card
    return None


def _set_audio_control(card: int, control: str, value, action: str = "set") -> None:
    result = subprocess.run([
        "amixer", "-q", "-c", str(card), "cset",
        f"iface=CARD,name={control}", str(value),
    ], capture_output=True, text=True, timeout=15, env=_clean_subprocess_env())
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        if "resource busy" in message.lower():
            raise RuntimeError("audio is currently playing; stop playback and try again")
        raise RuntimeError(f"could not {action} {control[:2]} audio DSP: {message}")


def _audio_control_value(card: int, control: str) -> str | None:
    result = subprocess.run([
        "amixer", "-c", str(card), "cget", f"iface=CARD,name={control}",
    ], capture_output=True, text=True, timeout=10, env=_clean_subprocess_env())
    if result.returncode:
        return None
    for line in reversed(result.stdout.splitlines()):
        if "values=" in line:
            return line.partition("values=")[2].strip()
    return None


def _set_audio_firmware_load(card: int, control: str, enabled: bool) -> None:
    _set_audio_control(card, control, "on" if enabled else "off", "reload")


def _audio_playback_active(card: int) -> bool:
    for status in Path(f"/proc/asound/card{card}").glob("pcm*p/sub*/status"):
        try:
            state = status.read_text().splitlines()[0].partition(":")[2].strip()
        except (OSError, IndexError):
            continue
        if state and state != "CLOSED":
            return True
    return False


def _wait_for_audio_idle(card: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _audio_playback_active(card) and time.monotonic() < deadline:
        time.sleep(0.25)
    if _audio_playback_active(card):
        raise RuntimeError("audio is currently playing; stop playback and try again")


def _deck_audio_command(arguments: list[str], timeout: float = 8.0):
    return subprocess.run([
        "/usr/bin/runuser", "-u", "deck", "--", "/usr/bin/env",
        "XDG_RUNTIME_DIR=/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        *arguments,
    ], capture_output=True, text=True, timeout=timeout, env=_clean_subprocess_env())


def _suspend_audio_outputs() -> list[str]:
    result = _deck_audio_command(["/usr/bin/pactl", "list", "short", "sinks"])
    if result.returncode:
        decky.logger.warning(f"{LOG} could not list PipeWire sinks: {result.stderr.strip()}")
        return []
    suspended = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        name = fields[1]
        changed = _deck_audio_command(["/usr/bin/pactl", "suspend-sink", name, "1"])
        if changed.returncode == 0:
            suspended.append(name)
        else:
            decky.logger.warning(f"{LOG} could not suspend sink {name}: {changed.stderr.strip()}")
    if suspended:
        time.sleep(0.35)
    return suspended


def _resume_audio_outputs(sinks: list[str]) -> None:
    for name in sinks:
        try:
            result = _deck_audio_command(["/usr/bin/pactl", "suspend-sink", name, "0"])
            if result.returncode:
                decky.logger.warning(f"{LOG} could not resume sink {name}: {result.stderr.strip()}")
        except Exception as error:
            decky.logger.warning(f"{LOG} could not resume sink {name}: {error}")


def _set_audio_services(running: bool) -> None:
    if running:
        result = _deck_audio_command([
            "/usr/bin/systemctl", "--user", "start", *AUDIO_USER_START_UNITS,
        ], timeout=20.0)
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"could not start PipeWire: {message}")
        time.sleep(0.6)
        return

    # A connected ALSA client can leave pipewire-pulse in "deactivating" for
    # its full stop timeout. Disable socket activation first, terminate the
    # disposable user daemons, then enqueue their stop jobs without waiting.
    commands = (
        ["/usr/bin/systemctl", "--user", "stop", "--no-block",
         "pipewire-pulse.socket", "pipewire.socket"],
        ["/usr/bin/systemctl", "--user", "kill", "--signal=SIGKILL",
         "pipewire-pulse.service", "wireplumber.service", "pipewire.service"],
        ["/usr/bin/systemctl", "--user", "stop", "--no-block",
         "pipewire-pulse.service", "wireplumber.service", "pipewire.service"],
    )
    for arguments in commands:
        result = _deck_audio_command(arguments, timeout=5.0)
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"could not release PipeWire audio: {message}")
    time.sleep(0.35)


@contextlib.contextmanager
def _audio_idle_session(card: int):
    """Temporarily release the physical PCM, then restore the user audio stack."""
    suspended = _suspend_audio_outputs() if _audio_playback_active(card) else []
    services_stopped = False
    try:
        if _audio_playback_active(card):
            services_stopped = True
            _set_audio_services(False)
        _wait_for_audio_idle(card)
        yield
    finally:
        if services_stopped:
            _set_audio_services(True)
        _resume_audio_outputs(suspended)


def _reload_audio_dsps_unlocked(card: int) -> None:
    disabled = []
    try:
        for control in AUDIO_FIRMWARE_CONTROLS:
            _set_audio_firmware_load(card, control, False)
            disabled.append(control)
        time.sleep(0.25)
        for control in AUDIO_FIRMWARE_CONTROLS:
            _set_audio_firmware_load(card, control, True)
            disabled.remove(control)
            time.sleep(0.25)
    finally:
        for control in disabled:
            try:
                _set_audio_firmware_load(card, control, True)
            except Exception as error:
                decky.logger.error(f"{LOG} could not recover {control}: {error}")


def _reload_audio_dsps(card: int) -> None:
    with _audio_idle_session(card):
        _reload_audio_dsps_unlocked(card)


def audio_fix_supported() -> bool:
    return (_audio_firmware_source() is not None and AUDIO_FIRMWARE_PATH.exists()
            and _audio_card_index() is not None)


def audio_fix_installed() -> bool:
    source = _audio_firmware_source()
    if source is None:
        return False
    try:
        configured = AUDIO_FIRMWARE_PATH.read_text().strip()
    except OSError:
        return False
    if configured != str(AUDIO_FIRMWARE_ROOT):
        return False
    return all(_same_file(source, AUDIO_FIRMWARE_ROOT / "cirrus" / name)
               for name in _audio_alias_filenames(source))


def audio_fix_ready() -> bool:
    if not audio_fix_installed():
        return False
    card = _audio_card_index()
    if card is None:
        return False
    return (
        all(_audio_control_value(card, control) == "on" for control in AUDIO_FIRMWARE_CONTROLS)
        and all(_audio_control_value(card, control) == "0"
                for control in AUDIO_FIRMWARE_TYPE_CONTROLS)
    )


def _apply_audio_fix_locked() -> None:
    if not supported_device():
        raise RuntimeError("audio fix is restricted to AYANEO 3")
    _prepare_audio_aliases()
    try:
        configured = AUDIO_FIRMWARE_PATH.read_text().strip()
    except OSError as error:
        raise RuntimeError(f"kernel firmware path is unavailable: {error}") from error
    allowed = {"", str(AUDIO_FIRMWARE_ROOT), *AUDIO_LEGACY_TEST_PATHS}
    if configured not in allowed:
        raise RuntimeError(f"another custom firmware path is active: {configured}")
    AUDIO_FIRMWARE_PATH.write_text(str(AUDIO_FIRMWARE_ROOT))
    card = _audio_card_index()
    if card is None:
        raise RuntimeError("AYANEO CS35L41 audio controls were not found")
    _reload_audio_dsps(card)


def apply_audio_fix() -> None:
    with _audio_apply_lock:
        _apply_audio_fix_locked()


def _remove_audio_fix_locked(reload_dsp: bool = True) -> None:
    configured = ""
    try:
        configured = AUDIO_FIRMWARE_PATH.read_text().strip()
        if configured == str(AUDIO_FIRMWARE_ROOT):
            AUDIO_FIRMWARE_PATH.write_text("")
    except OSError:
        pass
    try:
        if reload_dsp:
            card = _audio_card_index()
            if card is None:
                raise RuntimeError("AYANEO CS35L41 audio controls were not found")
            _reload_audio_dsps(card)
    except Exception:
        if configured == str(AUDIO_FIRMWARE_ROOT):
            with contextlib.suppress(OSError):
                AUDIO_FIRMWARE_PATH.write_text(str(AUDIO_FIRMWARE_ROOT))
        raise
    if AUDIO_FIRMWARE_ROOT.exists():
        shutil.rmtree(AUDIO_FIRMWARE_ROOT)


def remove_audio_fix(reload_dsp: bool = True) -> None:
    with _audio_apply_lock:
        _remove_audio_fix_locked(reload_dsp)


def _download_audio_calibration() -> bytes:
    request = urllib.request.Request(
        _checked_download_url(AUDIO_CALIBRATION_URL),
        headers={"User-Agent": "Ayaneo3Companion/0.6.1"},
    )
    with urllib.request.urlopen(request, context=_ssl_context(), timeout=30) as response:
        _checked_download_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > 64 * 1024:
            raise RuntimeError("audio calibration file is unexpectedly large")
        data = response.read(64 * 1024 + 1)
    if len(data) > 64 * 1024:
        raise RuntimeError("audio calibration file exceeded the download limit")
    if hashlib.sha256(data).hexdigest() != AUDIO_CALIBRATION_SHA256:
        raise RuntimeError("audio calibration file checksum mismatch")
    return data


def _prepare_audio_calibration_firmware() -> tuple[Path, Path]:
    wmfw_source = _audio_wmfw_source()
    if wmfw_source is None:
        raise RuntimeError("SteamOS AYANEO calibration firmware is missing")
    target_dir = AUDIO_FIRMWARE_ROOT / "cirrus"
    target_dir.mkdir(parents=True, exist_ok=True)

    wmfw_suffix = wmfw_source.name[len(AUDIO_FIRMWARE_WMFW_STEM):]
    wmfw_target = target_dir / f"{AUDIO_CALIBRATION_WMFW}{wmfw_suffix}"
    if not _same_file(wmfw_source, wmfw_target):
        temporary = wmfw_target.with_name(f".{wmfw_target.name}.tmp")
        shutil.copyfile(wmfw_source, temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, wmfw_target)

    bin_target = target_dir / AUDIO_CALIBRATION_BIN
    try:
        valid_bin = hashlib.sha256(bin_target.read_bytes()).hexdigest() == AUDIO_CALIBRATION_SHA256
    except OSError:
        valid_bin = False
    if not valid_bin:
        data = _download_audio_calibration()
        temporary = bin_target.with_name(f".{bin_target.name}.tmp")
        temporary.write_bytes(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, bin_target)
    return wmfw_target, bin_target


def _set_audio_firmware_type(card: int, profile: int) -> None:
    for control in AUDIO_FIRMWARE_CONTROLS:
        _set_audio_firmware_load(card, control, False)
    time.sleep(0.3)
    for control in AUDIO_FIRMWARE_TYPE_CONTROLS:
        _set_audio_control(card, control, profile, "select firmware for")
    for control in AUDIO_FIRMWARE_CONTROLS:
        _set_audio_firmware_load(card, control, True)
    time.sleep(1.0)


def _i2c_register_arguments(address: str, register: int) -> list[str]:
    return [f"0x{(register >> shift) & 0xff:02x}" for shift in (24, 16, 8, 0)]


def _read_audio_dsp_register(address: str, register: int) -> int:
    result = subprocess.run([
        "/usr/bin/i2ctransfer", "-f", "-y", "1", f"w4@{address}",
        *_i2c_register_arguments(address, register), "r4",
    ], capture_output=True, text=True, timeout=5, env=_clean_subprocess_env())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not read amplifier {address}")
    values = []
    for token in result.stdout.split():
        if token.lower().startswith("0x"):
            with contextlib.suppress(ValueError):
                values.append(int(token, 16))
    if len(values) != 4 or any(not 0 <= value <= 255 for value in values):
        raise RuntimeError(f"invalid amplifier response at {address}: {result.stdout.strip()}")
    return int.from_bytes(bytes(values), "big")


def _write_audio_dsp_register(address: str, register: int, value: int) -> None:
    data = [f"0x{byte:02x}" for byte in int(value).to_bytes(4, "big")]
    result = subprocess.run([
        "/usr/bin/i2ctransfer", "-f", "-y", "1", f"w8@{address}",
        *_i2c_register_arguments(address, register), *data,
    ], capture_output=True, text=True, timeout=5, env=_clean_subprocess_env())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not write amplifier {address}")


def _decode_audio_efi(blob: bytes) -> list[tuple[int, int, int, int, int]]:
    expected_size = 4 + AUDIO_EFI_PAYLOAD_SIZE
    if len(blob) != expected_size:
        raise RuntimeError(f"unexpected audio calibration EFI size: {len(blob)}")
    attributes = struct.unpack_from("<I", blob, 0)[0]
    size, count = AUDIO_EFI_HEADER.unpack_from(blob, 4)
    if attributes != AUDIO_EFI_ATTRIBUTES or size != AUDIO_EFI_PAYLOAD_SIZE or count != 2:
        raise RuntimeError("unexpected audio calibration EFI header")
    records = []
    for index in range(count):
        record = AUDIO_EFI_RECORD.unpack_from(blob, 4 + AUDIO_EFI_HEADER.size
                                              + index * AUDIO_EFI_RECORD.size)
        target, timestamp, _ambient, status, cal_r = record
        if not target or not timestamp or status != 1 or not cal_r:
            raise RuntimeError(f"invalid audio calibration EFI record {index}")
        records.append(record)
    return records


def _build_audio_efi(original: bytes, values: tuple[int, int], ambient: int) -> bytes:
    records = _decode_audio_efi(original)
    if not -128 <= ambient <= 127:
        raise RuntimeError("invalid ambient temperature")
    if any(not 4096 <= value <= 32767 for value in values):
        raise RuntimeError("measured speaker resistance is outside the safe range")
    filetime = time.time_ns() // 100 + 116444736000000000
    candidate = bytearray(original)
    for index, (record, cal_r) in enumerate(zip(records, values)):
        target = record[0]
        AUDIO_EFI_RECORD.pack_into(candidate, 4 + AUDIO_EFI_HEADER.size
                                   + index * AUDIO_EFI_RECORD.size,
                                   target, filetime, ambient, 1, cal_r)
    candidate_records = _decode_audio_efi(bytes(candidate))
    if [record[0] for record in candidate_records] != [record[0] for record in records]:
        raise RuntimeError("audio calibration target IDs changed unexpectedly")
    return bytes(candidate)


def _backup_audio_efi(original: bytes) -> Path:
    AUDIO_CALIBRATION_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(original).hexdigest()[:12]
    target = AUDIO_CALIBRATION_BACKUP_ROOT / f"CirrusSmartAmpCalibrationData-{stamp}-{digest}.bin"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, original)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if target.read_bytes() != original:
        raise RuntimeError("audio calibration backup verification failed")
    return target


def _write_audio_efi(path: Path, candidate: bytes, original: bytes) -> None:
    def write_blob(blob: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY)
        try:
            # efivarfs treats each write(2) as a complete SetVariable request.
            # It has no fsync operation, so fsync would report EINVAL even
            # after a successful firmware update.
            written = os.write(descriptor, blob)
            if written != len(blob):
                raise OSError(f"short EFI write: {written}/{len(blob)}")
        finally:
            os.close(descriptor)

    unlocked = subprocess.run(["/usr/bin/chattr", "-i", str(path)],
                              capture_output=True, text=True, timeout=10,
                              env=_clean_subprocess_env())
    if unlocked.returncode:
        raise RuntimeError(unlocked.stderr.strip() or "could not unlock audio calibration EFI")
    write_error = None
    try:
        try:
            write_blob(candidate)
            if path.read_bytes() != candidate:
                raise RuntimeError("audio calibration EFI readback mismatch")
        except Exception as error:
            write_error = error
            with contextlib.suppress(Exception):
                if path.read_bytes() != original:
                    write_blob(original)
    finally:
        locked = subprocess.run(["/usr/bin/chattr", "+i", str(path)],
                                capture_output=True, text=True, timeout=10,
                                env=_clean_subprocess_env())
    if locked.returncode:
        raise RuntimeError(locked.stderr.strip() or "could not relock audio calibration EFI")
    if write_error is not None:
        raise RuntimeError(f"could not save audio calibration: {write_error}") from write_error


def _measure_audio_calibration(card: int) -> tuple[int, int]:
    for _side, address in AUDIO_AMPLIFIERS:
        _write_audio_dsp_register(address, AUDIO_DSP_REGISTERS["cal_r"], 0)
        _write_audio_dsp_register(address, AUDIO_DSP_REGISTERS["ambient"],
                                  AUDIO_CALIBRATION_AMBIENT)
        _write_audio_dsp_register(address, AUDIO_DSP_REGISTERS["status"], 0)
        _write_audio_dsp_register(address, AUDIO_DSP_REGISTERS["checksum"], 0)

    started = time.monotonic()
    playback = subprocess.Popen([
        "/usr/bin/aplay", "-q", "-D", f"hw:{card},0", "-f", "S16_LE",
        "-c", "2", "-r", "48000", "/dev/zero",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
       env=_clean_subprocess_env())
    values = None
    try:
        deadline = started + 4.0
        while time.monotonic() < deadline:
            measured = []
            valid = True
            for _side, address in AUDIO_AMPLIFIERS:
                cal_r = _read_audio_dsp_register(address, AUDIO_DSP_REGISTERS["cal_r"])
                status = _read_audio_dsp_register(address, AUDIO_DSP_REGISTERS["status"])
                checksum = _read_audio_dsp_register(address, AUDIO_DSP_REGISTERS["checksum"])
                measured.append(cal_r)
                valid = valid and cal_r > 0 and status == 1 and checksum == cal_r + 1
            if valid:
                values = tuple(measured)
                break
            if playback.poll() is not None:
                error = playback.stderr.read().strip() if playback.stderr else ""
                raise RuntimeError(error or "audio calibration playback stopped unexpectedly")
            time.sleep(0.1)
        if values is None:
            raise RuntimeError("speaker calibration did not complete on both channels")
        remaining = started + 3.2 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        return values
    finally:
        if playback.poll() is None:
            playback.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                playback.wait(timeout=2)
        if playback.poll() is None:
            playback.kill()
            playback.wait(timeout=2)


def perform_audio_recalibration() -> dict:
    with _audio_apply_lock:
        if not supported_device() or not audio_fix_ready():
            raise RuntimeError("apply the AYANEO audio fix successfully before recalibrating")
        if not Path("/usr/bin/i2ctransfer").is_file() or not Path("/usr/bin/aplay").is_file():
            raise RuntimeError("SteamOS audio calibration tools are unavailable")
        efi_path = Path(AUDIO_CALIBRATION_EFI_GLOB)
        if not efi_path.is_file():
            raise RuntimeError("AYANEO speaker calibration EFI variable was not found")

        _prepare_audio_calibration_firmware()
        original = efi_path.read_bytes()
        records = _decode_audio_efi(original)
        previous = tuple(record[4] for record in records)
        backup = _backup_audio_efi(original)
        card = _audio_card_index()
        if card is None:
            raise RuntimeError("AYANEO CS35L41 audio controls were not found")

        power_controls = {}
        measured = None
        with _audio_idle_session(card):
            try:
                for power in Path("/sys/bus/i2c/devices").glob(
                        "i2c-CSC3551:00-cs35l41-hda.?/power/control"):
                    power_controls[power] = power.read_text().strip()
                    power.write_text("on")
                _set_audio_firmware_type(card, 1)
                measured = _measure_audio_calibration(card)
                decky.logger.info(
                    f"{LOG} measured speaker calibration "
                    f"L={measured[0]} R={measured[1]} at {AUDIO_CALIBRATION_AMBIENT} C")
                for value, old in zip(measured, previous):
                    if abs(value - old) > max(2048, round(old * 0.25)):
                        raise RuntimeError("measured speaker resistance changed by an unsafe amount")
                candidate = _build_audio_efi(original, measured, AUDIO_CALIBRATION_AMBIENT)
                _write_audio_efi(efi_path, candidate, original)
            finally:
                try:
                    _set_audio_firmware_type(card, 0)
                finally:
                    for power, value in power_controls.items():
                        with contextlib.suppress(OSError):
                            power.write_text(value)

        return {
            "left": measured[0], "right": measured[1],
            "ambient": AUDIO_CALIBRATION_AMBIENT,
            "backup": str(backup),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "restart_required": True,
        }


def _systemctl(*arguments, check: bool = False):
    return subprocess.run(["/usr/bin/systemctl", *arguments], check=check,
                          capture_output=True, text=True, timeout=20,
                          env=_clean_subprocess_env())


def ac_online() -> bool:
    """Return the real charger state, ignoring USB-C source-role supplies."""
    mains_seen = False
    for supply in glob.glob("/sys/class/power_supply/*"):
        try:
            if Path(supply, "type").read_text().strip() != "Mains":
                continue
            mains_seen = True
            if Path(supply, "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    if mains_seen:
        return False
    try:
        status = Path("/sys/class/power_supply/BAT0/status").read_text().strip()
        return status not in ("", "Discharging", "Unknown")
    except OSError:
        return False


def _power_supply_number(path: Path, name: str) -> int | None:
    try:
        return int((path / name).read_text().strip())
    except (OSError, ValueError):
        return None


def _upower_time_to_full(battery_name: str) -> int | None:
    """Read UPower's filtered charging estimate without keeping a D-Bus client."""
    if not battery_name.replace("_", "").isalnum():
        return None
    result = subprocess.run([
        "busctl", "--system", "get-property", "org.freedesktop.UPower",
        f"/org/freedesktop/UPower/devices/battery_{battery_name}",
        "org.freedesktop.UPower.Device", "TimeToFull",
    ], capture_output=True, text=True, timeout=3)
    if result.returncode:
        return None
    try:
        seconds = int(result.stdout.split()[-1])
    except (IndexError, ValueError):
        return None
    return seconds if seconds > 0 else None


def battery_status() -> dict:
    """Return live battery data and an estimated charging time."""
    battery = None
    try:
        for candidate in sorted(POWER_SUPPLY_ROOT.iterdir()):
            try:
                if (candidate / "type").read_text().strip() == "Battery":
                    battery = candidate
                    break
            except OSError:
                continue
    except OSError:
        pass
    if battery is None:
        return {
            "available": False, "percent": None, "status": "Unavailable",
            "seconds_to_full": None, "power_w": None, "source": "none",
        }

    try:
        status = (battery / "status").read_text().strip() or "Unknown"
    except OSError:
        status = "Unknown"
    percent = _power_supply_number(battery, "capacity")
    energy_now = _power_supply_number(battery, "energy_now")
    energy_full = _power_supply_number(battery, "energy_full")
    power_now = _power_supply_number(battery, "power_now")
    charge_now = _power_supply_number(battery, "charge_now")
    charge_full = _power_supply_number(battery, "charge_full")
    current_now = _power_supply_number(battery, "current_now")
    voltage_now = _power_supply_number(battery, "voltage_now")

    if percent is None and energy_now is not None and energy_full and energy_full > 0:
        percent = round(100 * energy_now / energy_full)
    if percent is not None:
        percent = max(0, min(100, percent))

    power_w = power_now / 1_000_000 if power_now is not None else None
    if power_w is None and current_now is not None and voltage_now is not None:
        power_w = current_now * voltage_now / 1_000_000_000_000
    if power_w is not None:
        power_w = round(abs(power_w), 2)

    seconds = None
    source = "none"
    if status.lower() == "charging":
        try:
            seconds = _upower_time_to_full(battery.name)
        except (OSError, subprocess.SubprocessError):
            seconds = None
        if seconds is not None:
            source = "UPower"
        elif energy_now is not None and energy_full is not None and power_now and power_now > 0:
            seconds = round(max(0, energy_full - energy_now) * 3600 / power_now)
            source = "sysfs"
        elif charge_now is not None and charge_full is not None and current_now and current_now > 0:
            seconds = round(max(0, charge_full - charge_now) * 3600 / current_now)
            source = "sysfs"
        if seconds is not None and not 0 < seconds <= 48 * 60 * 60:
            seconds = None
            source = "none"

    return {
        "available": True, "percent": percent, "status": status,
        "seconds_to_full": seconds, "power_w": power_w, "source": source,
    }


def _ec_io_path() -> Path | None:
    paths = sorted(Path("/sys/kernel/debug/ec").glob("ec*/io"))
    return paths[0] if paths else None


def ensure_charge_control() -> Path | None:
    """Load SteamOS' signed generic EC driver with writes enabled."""
    path = _ec_io_path()
    write_flag = Path("/sys/module/ec_sys/parameters/write_support")
    try:
        writable = write_flag.read_text().strip().lower() in ("y", "1")
    except OSError:
        writable = False
    if path is None or not writable:
        result = subprocess.run(["modprobe", "ec_sys", "write_support=1"],
                                capture_output=True, text=True)
        if result.returncode:
            decky.logger.warning(f"{LOG} cannot load ec_sys: {result.stderr.strip()}")
            return None
        path = _ec_io_path()
        try:
            writable = write_flag.read_text().strip().lower() in ("y", "1")
        except OSError:
            writable = False
    return path if writable else None


def read_charge_bypass() -> bool:
    path = _ec_io_path()
    if path is None:
        raise RuntimeError("AYANEO EC access is unavailable")
    with _ec_lock, path.open("rb", buffering=0) as ec:
        ec.seek(EC_CHARGE_REGISTER)
        value = ec.read(1)
    if value not in (bytes([EC_CHARGE_AUTO]), bytes([EC_CHARGE_INHIBIT])):
        raise RuntimeError(f"unexpected AYANEO charge register value: {value.hex() or 'empty'}")
    return value[0] == EC_CHARGE_INHIBIT


def write_charge_bypass(enabled: bool) -> None:
    if not supported_device():
        raise RuntimeError("charge bypass is restricted to AYANEO 3")
    path = ensure_charge_control()
    if path is None:
        raise RuntimeError("AYANEO EC charge control is unavailable")
    value = EC_CHARGE_INHIBIT if enabled else EC_CHARGE_AUTO
    with _ec_lock, path.open("r+b", buffering=0) as ec:
        ec.seek(EC_CHARGE_REGISTER)
        if ec.write(bytes([value])) != 1:
            raise RuntimeError("AYANEO EC rejected the charge setting")
    if read_charge_bypass() != enabled:
        raise RuntimeError("AYANEO EC did not retain the charge setting")


def _read_ec_register(register: int) -> int:
    path = _ec_io_path()
    if path is None:
        raise RuntimeError("AYANEO EC access is unavailable")
    with _ec_lock, path.open("rb", buffering=0) as ec:
        ec.seek(register)
        value = ec.read(1)
    if len(value) != 1:
        raise RuntimeError(f"could not read AYANEO EC register 0x{register:02x}")
    return value[0]


def _write_ec_register(register: int, value: int) -> None:
    path = ensure_charge_control()
    if path is None:
        raise RuntimeError("AYANEO EC write access is unavailable")
    with _ec_lock, path.open("r+b", buffering=0) as ec:
        ec.seek(register)
        if ec.write(bytes([value])) != 1:
            raise RuntimeError(f"could not write AYANEO EC register 0x{register:02x}")


def controller_powered() -> bool:
    return _read_ec_register(EC_CONTROLLER_POWER_REGISTER) == EC_CONTROLLER_POWER_ON


def both_modules_connected() -> bool:
    # Bits set in this register mean that the corresponding module is absent.
    return (_read_ec_register(EC_MODULE_REGISTER) & EC_MODULE_MASK) == 0


def module_presence() -> dict:
    """Return each physical slot state from the AYANEO EC."""
    value = _read_ec_register(EC_MODULE_REGISTER)
    return {
        "left": not bool(value & EC_MODULE_LEFT),
        "right": not bool(value & EC_MODULE_RIGHT),
    }


def _module_info(side: str, code: int | None = None, status: str = "connected") -> dict:
    labels = LEFT_MODULES if side == "left" else RIGHT_MODULES
    if code is None:
        label = {
            "detecting": "Detecting...",
            "activating": "Activating...",
            "ejecting": "Ejecting...",
            "unpowered": "Connected, unpowered",
            "disconnected": "Disconnected",
            "unavailable": "Unavailable",
        }.get(status, status.replace("_", " ").title())
        layout = ""
    else:
        label = labels.get(code, f"Unknown module (0x{code:02X})")
        layout = MODULE_LAYOUTS[side].get(code, "Layout not yet documented")
    return {
        "code": code,
        "label": label,
        "layout": layout,
        "status": status,
        "connected": status in ("connected", "detecting", "activating", "unpowered"),
    }


def module_states_from_presence(presence: dict) -> tuple[dict, dict]:
    """Mirror HHD's disconnected/unpowered status for incomplete module pairs."""
    left = bool(presence.get("left"))
    right = bool(presence.get("right"))
    if left and right:
        return _module_info("left", status="detecting"), _module_info("right", status="detecting")
    if left:
        return _module_info("left", status="unpowered"), _module_info("right", status="disconnected")
    if right:
        return _module_info("left", status="disconnected"), _module_info("right", status="unpowered")
    return _module_info("left", status="disconnected"), _module_info("right", status="disconnected")


def set_controller_power(enabled: bool) -> None:
    _write_ec_register(EC_CONTROLLER_POWER_REGISTER,
                       EC_CONTROLLER_POWER_ON if enabled else EC_CONTROLLER_POWER_OFF)


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def normalize_tdp(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    spl = _clamp(source.get("spl", DEFAULT_TDP["spl"]), 5, 35)
    sppt = _clamp(source.get("sppt", DEFAULT_TDP["sppt"]), spl, 37)
    fppt = _clamp(source.get("fppt", DEFAULT_TDP["fppt"]), sppt, 37)
    return {"spl": spl, "sppt": sppt, "fppt": fppt}


def _hex_color(value) -> str:
    text = str(value or "").strip().lstrip("#").lower()
    return text if len(text) == 6 and all(c in "0123456789abcdef" for c in text) else "6600ff"


def normalize_controller(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    vibration = str(source.get("vibration", "high")).lower()
    mode = str(source.get("rgb_mode", "solid")).lower()
    return {
        "vibration": vibration if vibration in VIBRATION_VALUES else "high",
        "ff_gain": _clamp(source.get("ff_gain", 100), 0, 100),
        "rgb_mode": mode if mode in RGB_MODES else "solid",
        "color": _hex_color(source.get("color")),
        "brightness": _clamp(source.get("brightness", 100), 0, 100),
    }


def _pad(data, length=65) -> bytes:
    return bytes(data).ljust(length, b"\0")


AYA_CHECK = _pad([0, 0, 0, 0, 0x08])
AYA_CUSTOM = _pad([0, 0, 0, 0, 0x0A, 0x02])
AYA_SAVE = _pad([0, 0, 0, 0, 0x05])
AYA_CUSTOM_REQUIRED_INDEX = 18


def controller_requires_custom(response: bytes) -> bool:
    # This status flag is not part of the one-byte-shifted RGB payload. Retail
    # HX 370 hardware and HHD both expose it at response byte 18.
    return len(response) > AYA_CUSTOM_REQUIRED_INDEX and \
        response[AYA_CUSTOM_REQUIRED_INDEX] == 1


def decode_module_layout(response: bytes) -> tuple[dict, dict]:
    """Decode the module identifiers returned by AYANEO's AYA_CHECK command."""
    if len(response) <= MODULE_INFO_RIGHT_INDEX:
        raise RuntimeError("controller response does not contain Magic Module information")
    left = response[MODULE_INFO_LEFT_INDEX]
    right = response[MODULE_INFO_RIGHT_INDEX]
    if not left or not right:
        raise RuntimeError("controller has not identified both Magic Modules yet")
    return _module_info("left", left), _module_info("right", right)


def _checksum(command) -> bytes:
    data = bytearray(_pad(command))
    data[1:3] = sum(data[7:]).to_bytes(2, "little")
    return bytes(data)


# AYANEO's 33-slot button table. These are USB HID keyboard usage IDs. Slots
# 0x12/0x13 are the rear LC1/RC1 buttons (L/R); 0x10/0x11 preserve LC/RC as
# F21/F22. The complete table must be initialized before individual slots emit.
AYA3_BUTTON_TABLE = {
    0x0C: (0x00, 0x00, 0x68),  # F13
    0x0D: (0x00, 0x00, 0x69),  # F14
    0x10: (0x00, 0x00, 0x70),  # F21 / LC
    0x11: (0x00, 0x00, 0x71),  # F22 / RC
    0x12: (0x02, 0x00, 0x0F),  # L / LC1
    0x13: (0x02, 0x00, 0x15),  # R / RC1
    0x16: (0x00, 0x00, 0x72),  # F23 / Guide
    0x17: (0x02, 0x08, 0x07),  # Left Meta + D / legacy QAM
    0x18: (0x00, 0x00, 0x6B),  # F16
}
AYA3_REAR_BUTTON_SLOTS = (0x12, 0x13)


def button_table_command(slot: int, binding=None) -> bytes:
    command = bytearray(65)
    command[3:6] = bytes((0x0B, 0x07, slot))
    if binding is not None:
        mode, modifier, usage = binding
        command[7] = mode
        command[10] = modifier
        command[12] = usage
    return _checksum(command)


def button_table_commands() -> tuple[bytes, ...]:
    return tuple(button_table_command(slot, AYA3_BUTTON_TABLE.get(slot))
                 for slot in range(0x21))


def rear_button_command(slot: int, usage: int | None) -> bytes:
    """Build an isolated rear-slot command for cleanup and protocol tests."""
    return button_table_command(slot, (0x02, 0x00, usage) if usage is not None else None)


def _rgb_bytes(config: dict):
    if config["rgb_mode"] == "off":
        return 0, 0, 0
    text = config["color"]
    r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h = (h + 10 / 360) % 1.0
    v = min(v, config["brightness"] / 100)
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def controller_command(config: dict, eject: str | None = None, reset: bool = False) -> bytes:
    config = normalize_controller(config)
    mode = RGB_MODES[config["rgb_mode"]]
    r, g, b = _rgb_bytes(config)
    vibration = VIBRATION_VALUES[config["vibration"]] << 4
    command = bytearray(65)
    command[3:5] = bytes((0x21, 0x09))
    command[8:12] = bytes((mode, r, g, b))
    command[12:16] = bytes((mode, r, g, b))
    command[20] = (0x88 if reset else
                   {None: 0x00, "left": 0x07, "right": 0x70, "both": 0x77}.get(eject, 0x00))
    command[22:25] = bytes((0x33, 0x22, vibration))
    command[32] = 1
    command[37:39] = bytes((0x64, 0x64))
    return _checksum(command)


def _vendor_hidraw() -> str:
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            descriptor = (Path(sys_path).resolve() / "device" / "report_descriptor").read_bytes()
            uevent = (Path(sys_path).resolve() / "device" / "uevent").read_text()
        except OSError:
            continue
        if descriptor.startswith(b"\x06\x00\xff\x09\x01") and "HID_ID=0003:00001C4F:00000002" in uevent:
            return "/dev/" + Path(sys_path).name
    raise RuntimeError("AYANEO vendor HID interface not found")


def _hid_exchange(fd, command: bytes, timeout=0.4) -> bytes:
    os.write(fd, command)
    for _ in range(3):
        ready, _, _ = select.select([fd], [], [], timeout)
        if ready:
            response = os.read(fd, 64)
            if len(response) > 3 and response[3] == command[4]:
                return response
    return b""


def _switch_to_custom_mode_fd(fd, response: bytes | None = None) -> bool:
    """Switch out of a TM-selected firmware mode and verify the transition."""
    current = response if response is not None else _hid_exchange(fd, AYA_CHECK)
    if not current:
        raise RuntimeError("controller did not answer the mode check")
    if not controller_requires_custom(current):
        return False
    if not _hid_exchange(fd, AYA_CUSTOM):
        raise RuntimeError("controller rejected custom mode")
    # The USB gamepad can briefly disappear while the controller changes mode.
    # Poll the vendor interface instead of relying on a fixed multi-second wait.
    for delay in (0.15, 0.35, 0.75, 1.25):
        time.sleep(delay)
        current = _hid_exchange(fd, AYA_CHECK)
        if current and not controller_requires_custom(current):
            return True
    raise RuntimeError("controller did not enter custom mode")


def read_module_layout() -> tuple[dict, dict]:
    path = _vendor_hidraw()
    with _controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            return decode_module_layout(_hid_exchange(fd, AYA_CHECK))
        finally:
            os.close(fd)


def program_rear_buttons(enabled: bool, config: dict | None = None) -> None:
    """Initialize AYANEO's button table and expose LC1/RC1 as L/R inputs."""
    if not supported_device():
        raise RuntimeError("rear button setup is restricted to AYANEO 3")
    path = _vendor_hidraw()
    with _controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            commands = (button_table_commands() if enabled else tuple(
                rear_button_command(slot, None) for slot in AYA3_REAR_BUTTON_SLOTS))
            acknowledged = 0
            for command in commands:
                if _hid_exchange(fd, command):
                    acknowledged += 1
            if acknowledged != len(commands):
                raise RuntimeError(
                    f"controller accepted only {acknowledged}/{len(commands)} button-table entries")
            if enabled:
                current = normalize_controller(
                    config if config is not None else settings.getSetting("controller", DEFAULT_CONTROLLER))
                if not _hid_exchange(fd, controller_command(current, reset=True)):
                    raise RuntimeError("controller rejected button-table activation reset")
                time.sleep(0.5)
                if not _hid_exchange(fd, controller_command(current)):
                    raise RuntimeError("controller rejected configuration restore")
            if not _hid_exchange(fd, AYA_SAVE, timeout=1.5):
                raise RuntimeError("controller did not save LC1/RC1 firmware bindings")
        finally:
            os.close(fd)


def read_controller() -> dict:
    path = _vendor_hidraw()
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    try:
        response = _hid_exchange(fd, AYA_CHECK)
    finally:
        os.close(fd)
    if len(response) < 25:
        raise RuntimeError("controller did not answer")
    reverse_modes = {value: key for key, value in RGB_MODES.items()}
    reverse_vibration = {value: key for key, value in VIBRATION_VALUES.items()}
    # Linux hidraw strips the zero report-ID byte from input reports, so every
    # response field is one byte earlier than its output-command counterpart.
    r, g, b = (value / 255 for value in response[8:11])
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    # The packet stores the +10 degree colour-corrected hue. Undo it so
    # reading and re-applying a setting is byte-stable.
    r, g, b = (round(value * 255) for value in colorsys.hsv_to_rgb((h - 10 / 360) % 1.0, s, v))
    return {
        "vibration": reverse_vibration.get(response[23] >> 4, "high"),
        "ff_gain": 100,
        "rgb_mode": reverse_modes.get(response[7], "solid"),
        "color": bytes((r, g, b)).hex(),
        "brightness": 100,
    }


def apply_controller(config: dict, vibration_feedback: bool = False,
                     previous_vibration: str | None = None,
                     persist_firmware: bool = True) -> None:
    config = normalize_controller(config)
    feedback_level = config["vibration"]
    with _controller_apply_lock:
        # Once Off is applied the firmware ignores force-feedback, so confirm
        # that transition with the previous level immediately beforehand.
        if (vibration_feedback and feedback_level == "off"
                and previous_vibration in ("low", "medium", "high")):
            play_vibration_test(previous_vibration, VIBRATION_CONFIRM_MS)
        path = _vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            if not _hid_exchange(fd, controller_command(config)):
                raise RuntimeError("controller rejected configuration")
            # The configuration command applies the new level immediately.
            # Confirm it before the slower non-volatile save operation.
            if vibration_feedback and feedback_level != "off":
                play_vibration_test(feedback_level, VIBRATION_CONFIRM_MS)
            # The plugin persists every setting itself. Firmware save is useful
            # for ordinary RGB/config writes, but it adds a second, much longer
            # controller-side confirmation when changing vibration strength.
            if persist_firmware:
                _hid_exchange(fd, AYA_SAVE, timeout=1.5)
        finally:
            os.close(fd)


def eject_controller_modules(side: str, config: dict) -> None:
    if side not in ("left", "right", "both"):
        raise ValueError("invalid controller module selection")
    if not supported_device():
        raise RuntimeError("module eject is restricted to AYANEO 3")
    with _controller_apply_lock:
        path = _vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        started = time.monotonic()
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            if not _hid_exchange(fd, controller_command(config, side)):
                raise RuntimeError("controller rejected the eject command")
            # Match HHD's module-release verification before cutting controller
            # power so the stepper motor can finish moving the latch.
            for _ in range(20):
                response = _hid_exchange(fd, AYA_CHECK)
                time.sleep(0.4)
                if len(response) > 19 and response[19] & ~0x11 == 0:
                    break
        finally:
            os.close(fd)
        remaining = 3.0 - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
        set_controller_power(False)


def reset_controller_modules(config: dict, restore_buttons: bool) -> None:
    """Re-initialize both Magic Modules and restore all volatile controller state."""
    if not supported_device():
        raise RuntimeError("module reset is restricted to AYANEO 3")
    if not both_modules_connected():
        raise RuntimeError("insert both Magic Modules before resetting")
    if not controller_powered():
        set_controller_power(True)
        time.sleep(0.75)
    path = _vendor_hidraw()
    with _controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            if not _hid_exchange(fd, controller_command(config, reset=True)):
                raise RuntimeError("controller rejected the Magic Module reset")
            time.sleep(0.5)
            if not _hid_exchange(fd, controller_command(config)):
                raise RuntimeError("controller did not return after the Magic Module reset")
        finally:
            os.close(fd)
    if restore_buttons:
        program_rear_buttons(True, config)
    apply_controller(config)
    set_vibration_gain(config["ff_gain"])


def recover_tm_mode(config: dict, _restore_buttons: bool) -> bool:
    """Undo a hardware TM mode change and restore the Companion configuration."""
    if not both_modules_connected() or not controller_powered():
        return False
    path = _vendor_hidraw()
    with _controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            changed = _switch_to_custom_mode_fd(fd, check)
        finally:
            os.close(fd)
    if not changed:
        return False
    # AYA_SAVE keeps the complete button table in controller NVRAM. Rewriting
    # it here would send the 0x88 activation reset and cycle the physical Magic
    # Module mechanisms after every accidental TM mode change.
    apply_controller(config)
    set_vibration_gain(config["ff_gain"])
    return True


def _event_has_rumble(node: str) -> bool:
    try:
        import fcntl
        with open(node, "rb") as handle:
            bits = bytearray(32)
            fcntl.ioctl(handle.fileno(), EVIOCGBIT_FF, bits)
            return bool(bits[FF_RUMBLE // 8] & (1 << (FF_RUMBLE % 8)))
    except (ImportError, OSError):
        return False


def _rumble_event_node() -> str | None:
    """Prefer AYANEO's physical input node over virtual rumble devices."""
    fallback = None
    nodes = sorted(glob.glob("/dev/input/event*"),
                   key=lambda path: int("".join(filter(str.isdigit, Path(path).name)) or 0))
    for node in nodes:
        if not _event_has_rumble(node):
            continue
        try:
            vendor = Path(f"/sys/class/input/{Path(node).name}/device/id/vendor").read_text().strip().lower()
        except OSError:
            vendor = ""
        # The controller firmware exposes an Xbox 360-compatible xpad node
        # (045e:028e). Never prefer InputPlumber's 28de virtual controller,
        # because FF_GAIN must scale the physical device itself.
        if vendor in ("045e", "1c4f"):
            return node
        fallback = fallback or node
    return fallback


def set_vibration_gain(percent: int) -> None:
    """Set Linux FF_GAIN on AYANEO's physical gamepad input device."""
    gain = _clamp(percent, 0, 100)
    node = _rumble_event_node()
    if not node:
        raise RuntimeError("No rumble-capable input device found")
    now = time.time()
    event = struct.pack("<qqHHi", int(now), int((now % 1) * 1_000_000),
                        EV_FF, FF_GAIN, round(0xFFFF * gain / 100))
    fd = os.open(node, os.O_RDWR)
    try:
        if os.write(fd, event) != len(event):
            raise RuntimeError("Controller rejected FF_GAIN")
    finally:
        os.close(fd)


def play_vibration_test(level: str, duration_ms: int = 500) -> None:
    """Play one FF_RUMBLE effect without changing the saved firmware level."""
    import fcntl
    import time

    # Keep confirmation levels perceptually distinct. Small rumble motors are
    # strongly non-linear, so evenly spaced numeric values feel too similar.
    strength = {"low": 0.20, "medium": 0.55, "high": 1.0}.get(level, 0.0)
    if strength <= 0:
        raise RuntimeError("Vibration is Off - select a strength first")
    node = _rumble_event_node()
    if not node:
        raise RuntimeError("No rumble-capable input device found")
    duration = max(100, min(2000, int(duration_ms)))
    magnitude = round(0xFFFF * strength)
    fd = os.open(node, os.O_RDWR)
    try:
        effect = bytearray(struct.pack("<HhHHHHHxxHH28x", FF_RUMBLE, -1, 0,
                                       0, 0, duration, 0, magnitude, magnitude))
        fcntl.ioctl(fd, EVIOCSFF, effect)
        effect_id = struct.unpack_from("<h", effect, 2)[0]
        if effect_id < 0:
            raise RuntimeError("Controller rejected the vibration effect")

        def event(value: int) -> bytes:
            now = time.time()
            return struct.pack("<qqHHi", int(now), int((now % 1) * 1_000_000),
                               EV_FF, effect_id, value)

        os.write(fd, event(1))
        time.sleep(duration / 1000)
        os.write(fd, event(0))
        fcntl.ioctl(fd, EVIOCRMFF, effect_id)
    finally:
        os.close(fd)


def _powerstation_card():
    if subprocess.run(["busctl", "--system", "status", "org.shadowblip.PowerStation"],
                      capture_output=True).returncode != 0:
        return None
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        path = f"/org/shadowblip/Performance/GPU/{Path(card).name}"
        probe = subprocess.run(["busctl", "--system", "get-property", "org.shadowblip.PowerStation",
                                path, "org.shadowblip.GPU.Card.TDP", "TDP"], capture_output=True)
        if probe.returncode == 0:
            return path
    return None


def _checked_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS:
        raise RuntimeError("refusing untrusted RyzenAdj download URL")
    return url


def _ssl_context():
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca"):
        _ssl_ctx = context
        return context
    for path in CA_BUNDLES:
        try:
            if os.path.exists(path):
                context.load_verify_locations(cafile=path)
                if context.cert_store_stats().get("x509_ca"):
                    decky.logger.info(f"{LOG} TLS: loaded CA bundle {path}")
                    _ssl_ctx = context
                    return context
        except OSError as error:
            decky.logger.warning(f"{LOG} TLS: cannot load {path}: {error}")
    raise RuntimeError("no usable TLS CA bundle found")


def _download_archive(target: Path) -> None:
    request = urllib.request.Request(
        _checked_download_url(RYZENADJ_URL),
        headers={"User-Agent": "Ayaneo3Companion/0.4.4"},
    )
    with urllib.request.urlopen(request, context=_ssl_context(), timeout=30) as response:
        _checked_download_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("RyzenAdj archive is unexpectedly large")
        total = 0
        with target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("RyzenAdj archive exceeded the download limit")
                output.write(chunk)


def _ensure_ryzenadj() -> None:
    if RYZENADJ.exists() and hashlib.sha256(RYZENADJ.read_bytes()).hexdigest() == RYZENADJ_BINARY_SHA256:
        return
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        archive = Path(temp) / "ryzenadj.tar.gz"
        _download_archive(archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != RYZENADJ_ARCHIVE_SHA256:
            raise RuntimeError("RyzenAdj archive checksum mismatch")
        with tarfile.open(archive, "r:gz") as bundle:
            for name, target in (("ryzenadj", RYZENADJ), ("libryzenadj.so", RYZENADJ_LIB)):
                member = next((item for item in bundle.getmembers() if Path(item.name).name == name), None)
                if member is None or not member.isfile():
                    raise RuntimeError(f"RyzenAdj archive is missing {name}")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"could not extract {name}")
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    if hashlib.sha256(RYZENADJ.read_bytes()).hexdigest() != RYZENADJ_BINARY_SHA256:
        raise RuntimeError("RyzenAdj binary checksum mismatch")
    RYZENADJ.chmod(0o755)


def tdp_backend() -> str:
    return "PowerStation" if _powerstation_card() else "RyzenAdj"


def _apply_tdp_unlocked(config: dict) -> None:
    values = normalize_tdp(config)
    card = _powerstation_card()
    if card:
        # PowerStation exposes sustained TDP plus boost headroom. Its backend
        # derives fast PPT, so keep the user-facing three-limit relationship.
        boost = max(0, values["sppt"] - values["spl"])
        for prop, value in (("TDP", values["spl"]), ("Boost", boost)):
            result = subprocess.run(["busctl", "--system", "set-property", "org.shadowblip.PowerStation",
                                     card, "org.shadowblip.GPU.Card.TDP", prop, "d", str(float(value))],
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or f"could not set {prop}")
        return
    _ensure_ryzenadj()
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(BIN_DIR)
    result = subprocess.run([str(RYZENADJ), f"--stapm-limit={values['spl'] * 1000}",
                             f"--slow-limit={values['sppt'] * 1000}",
                             f"--fast-limit={values['fppt'] * 1000}"],
                            capture_output=True, text=True, timeout=10, env=env)
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


def _is_our_display_script(path: Path) -> bool:
    try:
        return (path.read_bytes().replace(b"\r\n", b"\n") ==
                LUA_SOURCE.read_bytes().replace(b"\r\n", b"\n"))
    except OSError:
        return False


def _cta_luminance_code(nits: float) -> int:
    return max(1, min(255, round(32 * math.log2(float(nits) / 50))))


def patch_ayaneo_edid(data: bytes, nits: float = EDID_TARGET_NITS):
    """Return an AYANEO 3 EDID with CTA MaxCLL corrected, or None."""
    if (len(data) < 256 or len(data) % 128 or
            data[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00" or
            data[8:12] != b"\x07\x21\x13\x01"):
        return None
    wanted = _cta_luminance_code(nits)
    result = bytearray(data)
    for start in range(128, len(result), 128):
        block = result[start:start + 128]
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
                block[pos + 4] = wanted
                block[127] = (-sum(block[:127])) & 0xff
                result[start:start + 128] = block
                return bytes(result)
            pos = end
    return None


def _published_edid_nits(data: bytes):
    if len(data) < 256 or len(data) % 128:
        return None
    for start in range(128, len(data), 128):
        block = data[start:start + 128]
        if block[0] != 0x02:
            continue
        dtd_start = block[2] or 127
        pos = 4
        while 4 <= pos < dtd_start:
            header = block[pos]
            length = header & 0x1f
            end = pos + 1 + length
            if end > dtd_start:
                return None
            if header >> 5 == 7 and length >= 4 and block[pos + 1] == 6:
                code = block[pos + 4]
                return 50 * (2 ** (code / 32)) if code else None
            pos = end
    return None


def patch_published_edid() -> bool:
    """Safely correct gamescope's user-owned EDID copy in place."""
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(PUBLISHED_EDID, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 1000:
            return False
        data = os.read(fd, info.st_size)
        patched = patch_ayaneo_edid(data)
        if patched is None:
            return False
        if patched != data:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, patched)
            os.ftruncate(fd, len(patched))
            os.fsync(fd)
            decky.logger.info(f"{LOG} patched published EDID MaxCLL to {EDID_TARGET_NITS} nits")
        return True
    finally:
        os.close(fd)


def button_map_bytes() -> bytes:
    """Return the aya7 extension with both native and legacy QAM inputs."""
    return INPUT_MAP_SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


def button_fix_installed() -> bool:
    try:
        return INPUT_MAP_TARGET.read_bytes() == button_map_bytes()
    except OSError:
        return False


def install_button_fix() -> None:
    """Extend native aya7 without replacing its APU-specific device profile."""
    INPUT_MAP_TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = INPUT_MAP_TARGET.with_name(f".{INPUT_MAP_TARGET.name}.tmp")
    temporary.write_bytes(button_map_bytes())
    os.chmod(temporary, 0o644)
    os.replace(temporary, INPUT_MAP_TARGET)
    # InputPlumber sorts maps globally by filename and the last duplicate ID
    # wins. Matching the stock ayaneo_type7.yaml filename makes the /etc copy
    # sort after /usr/share via directory priority.
    # Remove the old full-device override. It hard-coded the 8840U USB path and
    # unnecessarily replaced working native mappings on the HX 370 variant.
    for legacy in (LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        try:
            legacy.unlink()
        except FileNotFoundError:
            pass


def remove_button_fix() -> None:
    for target in (INPUT_MAP_TARGET, LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        try:
            target.unlink()
        except FileNotFoundError:
            pass


class Plugin:
    _state = {}
    _restore_task = None
    _edid_task = None
    _ac_task = None
    _module_task = None
    _tm_guard_task = None
    _audio_task = None
    _active_app = ""

    @staticmethod
    def _save(key, value):
        settings.setSetting(key, value)
        settings.commit()

    @classmethod
    def _snapshot(cls):
        with _lock:
            state = dict(cls._state)
            state["tdp"] = dict(state["tdp"])
            state["controller"] = dict(state["controller"])
            state["module_left"] = dict(state["module_left"])
            state["module_right"] = dict(state["module_right"])
        # Hardware reads can involve sysfs, debugfs and subprocesses. Keep them
        # outside the shared state lock so the monitor loops and RPC writes do
        # not stall behind the QAM's periodic refresh.
        state["gpu_power_w"] = gpu_power_watts()
        state["screen_installed"] = _is_our_display_script(LUA_TARGET)
        try:
            edid = PUBLISHED_EDID.read_bytes()
            state["edid_game_nits"] = round(_published_edid_nits(edid) or 0)
        except OSError:
            state["edid_game_nits"] = 0
        state["edid_patched"] = state["edid_game_nits"] == EDID_TARGET_NITS
        state["button_fix_installed"] = button_fix_installed()
        try:
            state["charge_bypass"] = read_charge_bypass()
            state["charge_bypass_supported"] = True
        except (OSError, RuntimeError):
            state["charge_bypass_supported"] = False
        try:
            presence = module_presence()
            state["modules_connected"] = all(presence.values())
            state["module_eject_supported"] = True
            state["module_reset_supported"] = True
            if not state["modules_connected"]:
                state["module_left"], state["module_right"] = \
                    module_states_from_presence(presence)
        except (OSError, RuntimeError):
            state["module_eject_supported"] = False
            state["module_reset_supported"] = False
        return state

    async def get_state(self):
        return await asyncio.to_thread(self._snapshot)

    async def get_battery_status(self):
        return await asyncio.to_thread(battery_status)

    async def set_tdp(self, raw):
        value = normalize_tdp(raw)
        await asyncio.to_thread(apply_tdp, value)
        with _lock:
            Plugin._state["tdp"] = value
            self._save("tdp", value)
        return await self.get_state()

    async def get_game_profile(self, app_id):
        profiles = settings.getSetting("game_profiles", {})
        value = profiles.get(str(app_id)) if isinstance(profiles, dict) else None
        return {"exists": isinstance(value, dict),
                "profile": normalize_tdp(value) if isinstance(value, dict) else {}}

    async def set_game_profile(self, app_id, raw):
        app_id = str(app_id or "")
        if not app_id or app_id != Plugin._active_app:
            raise RuntimeError("game is no longer active")
        value = normalize_tdp(raw)
        await asyncio.to_thread(apply_tdp, value)
        with _lock:
            profiles = settings.getSetting("game_profiles", {})
            profiles = dict(profiles) if isinstance(profiles, dict) else {}
            profiles[app_id] = value
            settings.setSetting("game_profiles", profiles)
            settings.commit()
            Plugin._state["tdp"] = value
        return await self.get_state()

    async def delete_game_profile(self, app_id):
        app_id = str(app_id or "")
        profiles = settings.getSetting("game_profiles", {})
        profiles = dict(profiles) if isinstance(profiles, dict) else {}
        profiles.pop(app_id, None)
        target = normalize_tdp(settings.getSetting("tdp", DEFAULT_TDP))
        if app_id and app_id == Plugin._active_app:
            await asyncio.to_thread(apply_tdp, target)
        with _lock:
            settings.setSetting("game_profiles", profiles)
            settings.commit()
            if app_id == Plugin._active_app:
                Plugin._state["tdp"] = target
        return await self.get_state()

    async def set_active_app(self, app_id):
        app_id = str(app_id or "")
        if app_id == Plugin._active_app:
            return
        profiles = settings.getSetting("game_profiles", {})
        profile = profiles.get(app_id) if app_id and isinstance(profiles, dict) else None
        target = normalize_tdp(profile if isinstance(profile, dict)
                               else settings.getSetting("tdp", DEFAULT_TDP))
        await asyncio.to_thread(apply_tdp, target)
        with _lock:
            Plugin._active_app = app_id
            Plugin._state["tdp"] = target
        decky.logger.info(f"{LOG} applied {'game ' + app_id if profile else 'global'} TDP")

    async def set_controller(self, raw):
        value = normalize_controller(raw)
        await asyncio.to_thread(apply_controller, value)
        with _lock:
            Plugin._state["controller"] = value
            self._save("controller", value)
        return await self.get_state()

    async def set_controller_with_vibration_feedback(self, raw):
        value = normalize_controller(raw)
        with _lock:
            previous_vibration = Plugin._state["controller"]["vibration"]
        await asyncio.to_thread(apply_controller, value, True, previous_vibration, False)
        with _lock:
            Plugin._state["controller"] = value
            self._save("controller", value)
        return await self.get_state()

    async def set_vibration_gain(self, percent):
        value = _clamp(percent, 0, 100)
        await asyncio.to_thread(set_vibration_gain, value)
        with _lock:
            controller = dict(Plugin._state["controller"])
            controller["ff_gain"] = value
            Plugin._state["controller"] = controller
            self._save("controller", controller)
        return await self.get_state()

    async def test_vibration(self, duration_ms=VIBRATION_TEST_MS):
        with _lock:
            level = Plugin._state["controller"]["vibration"]
        try:
            await asyncio.to_thread(play_vibration_test, level, duration_ms)
            return {"success": True}
        except Exception as error:
            decky.logger.error(f"{LOG} vibration test failed: {error}")
            return {"success": False, "error": str(error)}

    async def set_charge_bypass(self, enabled):
        value = bool(enabled)
        await asyncio.to_thread(write_charge_bypass, value)
        with _lock:
            Plugin._state["charge_bypass"] = value
            Plugin._state["charge_bypass_supported"] = True
            self._save("charge_bypass", value)
        return await self.get_state()

    async def set_audio_fix(self, enabled):
        value = bool(enabled)
        with _lock:
            previous = bool(Plugin._state.get("audio_fix_enabled", False))
            Plugin._state["audio_fix_error"] = ""
        try:
            if value:
                await asyncio.to_thread(apply_audio_fix)
            else:
                await asyncio.to_thread(remove_audio_fix)
            installed = await asyncio.to_thread(audio_fix_installed)
            ready = await asyncio.to_thread(audio_fix_ready) if value else False
            if value and not ready:
                raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
            with _lock:
                Plugin._state["audio_fix_enabled"] = value
                Plugin._state["audio_fix_installed"] = installed
                Plugin._state["audio_calibration_available"] = value and ready
                Plugin._state["audio_profile"] = "AYANEO v0.65" if value else "Generic fallback"
                self._save("audio_fix_enabled", value)
        except Exception as error:
            installed = await asyncio.to_thread(audio_fix_installed)
            ready = await asyncio.to_thread(audio_fix_ready) if installed else False
            with _lock:
                Plugin._state["audio_fix_enabled"] = previous
                Plugin._state["audio_fix_error"] = str(error)
                Plugin._state["audio_fix_installed"] = installed
                Plugin._state["audio_calibration_available"] = previous and ready
                Plugin._state["audio_profile"] = (
                    "AYANEO v0.65" if ready else
                    ("Installed, not active" if installed else "Generic fallback"))
            raise
        return await self.get_state()

    async def reapply_audio_fix(self):
        with _lock:
            enabled = Plugin._state.get("audio_fix_enabled", False)
        if not enabled:
            raise RuntimeError("enable the AYANEO audio tuning first")
        return await self.set_audio_fix(True)

    async def recalibrate_audio(self):
        with _lock:
            available = bool(Plugin._state.get("audio_calibration_available", False))
        if not available or not await asyncio.to_thread(audio_fix_ready):
            raise RuntimeError("apply the AYANEO audio fix successfully before recalibrating")
        try:
            result = await asyncio.to_thread(perform_audio_recalibration)
            summary = (
                f"{result['timestamp']} · L {result['left']} · R {result['right']} · "
                f"{result['ambient']} °C · Restart required")
            with _lock:
                Plugin._state["audio_calibration_available"] = True
                Plugin._state["audio_calibration_last"] = summary
                Plugin._state["audio_fix_error"] = ""
                self._save("audio_calibration_last", summary)
            decky.logger.info(
                f"{LOG} saved speaker calibration L={result['left']} R={result['right']} "
                f"backup={result['backup']}")
        except Exception as error:
            ready = await asyncio.to_thread(audio_fix_ready)
            with _lock:
                Plugin._state["audio_calibration_available"] = ready
                Plugin._state["audio_fix_error"] = str(error)
            raise
        return await self.get_state()

    async def eject_modules(self, side):
        side = str(side or "").lower()
        with _lock:
            if Plugin._state.get("modules_reconnecting"):
                raise RuntimeError("reinsert both modules before ejecting again")
            controller = dict(Plugin._state["controller"])
        await asyncio.to_thread(eject_controller_modules, side, controller)
        with _lock:
            Plugin._state["modules_reconnecting"] = True
            Plugin._state["modules_connected"] = False
            if side in ("left", "both"):
                Plugin._state["module_left"] = _module_info("left", status="ejecting")
            if side in ("right", "both"):
                Plugin._state["module_right"] = _module_info("right", status="ejecting")
        decky.logger.info(f"{LOG} ejected {side} controller module(s)")
        return await self.get_state()

    async def reset_modules(self):
        with _lock:
            if Plugin._state.get("modules_reconnecting"):
                raise RuntimeError("insert both modules before resetting")
            controller = dict(Plugin._state["controller"])
            restore_buttons = Plugin._state.get("button_fix_installed", False)
            Plugin._state["module_left"] = _module_info("left", status="activating")
            Plugin._state["module_right"] = _module_info("right", status="activating")
        try:
            await asyncio.to_thread(reset_controller_modules, controller, restore_buttons)
            left, right = await asyncio.to_thread(read_module_layout)
            with _lock:
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
                Plugin._state["modules_connected"] = True
                Plugin._state["modules_reconnecting"] = False
            decky.logger.info(f"{LOG} reset and re-detected both Magic Modules")
        except Exception:
            try:
                presence = await asyncio.to_thread(module_presence)
                left, right = module_states_from_presence(presence)
                with _lock:
                    Plugin._state["module_left"] = left
                    Plugin._state["module_right"] = right
            except Exception:
                pass
            raise
        return await self.get_state()

    async def set_screen_fix(self, enabled):
        if enabled:
            LUA_TARGET.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(LUA_SOURCE, LUA_TARGET)
            await asyncio.to_thread(patch_published_edid)
        elif _is_our_display_script(LUA_TARGET):
            LUA_TARGET.unlink()
        return await self.get_state()

    async def _edid_loop(self):
        """Reapply after every gamescope session recreates its EDID copy."""
        while True:
            try:
                if _is_our_display_script(LUA_TARGET):
                    await asyncio.to_thread(patch_published_edid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} EDID correction failed: {error}")
            await asyncio.sleep(1)

    async def set_button_fix(self, enabled):
        if enabled:
            with _lock:
                controller = dict(Plugin._state["controller"])
            await asyncio.to_thread(program_rear_buttons, True, controller)
            await asyncio.to_thread(install_button_fix)
        else:
            await asyncio.to_thread(program_rear_buttons, False)
            await asyncio.to_thread(remove_button_fix)
        await asyncio.to_thread(_systemctl, "restart", "inputplumber", check=True)
        with _lock:
            Plugin._state["button_fix_installed"] = bool(enabled)
        return await self.get_state()

    async def set_tm_guard(self, enabled):
        value = bool(enabled)
        with _lock:
            Plugin._state["tm_guard_enabled"] = value
            Plugin._state["tm_guard_status"] = "Monitoring" if value else "Disabled"
            self._save("tm_guard_enabled", value)
        if value:
            with _lock:
                controller = dict(Plugin._state["controller"])
                restore_buttons = Plugin._state.get("button_fix_installed", False)
            try:
                changed = await asyncio.to_thread(recover_tm_mode, controller, restore_buttons)
            except Exception as error:
                with _lock:
                    Plugin._state["tm_guard_status"] = "Waiting for controller"
                decky.logger.debug(f"{LOG} TM Guard initial check pending: {error}")
            else:
                if changed:
                    with _lock:
                        Plugin._state["tm_guard_recoveries"] += 1
                        Plugin._state["tm_guard_status"] = "Custom mode restored"
        return await self.get_state()

    async def _restore_hardware(self):
        """Reapply persisted volatile hardware settings once boot services settle."""
        saved_bypass = settings.getSetting("charge_bypass", None)
        pending = {"tdp", "controller"}
        if isinstance(saved_bypass, bool):
            pending.add("charge_bypass")
        for delay in (1, 2, 4, 8):
            await asyncio.sleep(delay)
            with _lock:
                tdp = dict(Plugin._state["tdp"])
                controller = dict(Plugin._state["controller"])
            if "tdp" in pending:
                try:
                    await asyncio.to_thread(apply_tdp, tdp)
                    pending.remove("tdp")
                    decky.logger.info(f"{LOG} restored TDP after startup")
                except Exception as error:
                    decky.logger.warning(f"{LOG} TDP restore attempt failed: {error}")
            if "controller" in pending:
                try:
                    await asyncio.to_thread(apply_controller, controller)
                    await asyncio.to_thread(set_vibration_gain, controller["ff_gain"])
                    pending.remove("controller")
                    decky.logger.info(f"{LOG} restored RGB, firmware vibration and FF_GAIN after startup")
                except Exception as error:
                    decky.logger.warning(f"{LOG} controller restore attempt failed: {error}")
            if "charge_bypass" in pending:
                try:
                    await asyncio.to_thread(write_charge_bypass, saved_bypass)
                    with _lock:
                        Plugin._state["charge_bypass"] = saved_bypass
                        Plugin._state["charge_bypass_supported"] = True
                    pending.remove("charge_bypass")
                    decky.logger.info(f"{LOG} restored charge bypass after startup")
                except Exception as error:
                    decky.logger.warning(f"{LOG} charge bypass restore attempt failed: {error}")
            if not pending:
                return
        decky.logger.error(f"{LOG} could not restore after startup: {', '.join(sorted(pending))}")

    async def _restore_audio(self):
        """Load the device-specific tuning after ALSA exposes both DSP controls."""
        for delay in (1, 2, 4, 8):
            await asyncio.sleep(delay)
            with _lock:
                if not Plugin._state.get("audio_fix_enabled", False):
                    return
            try:
                if await asyncio.to_thread(audio_fix_ready):
                    with _lock:
                        Plugin._state["audio_fix_supported"] = True
                        Plugin._state["audio_fix_installed"] = True
                        Plugin._state["audio_calibration_available"] = True
                        Plugin._state["audio_profile"] = "AYANEO v0.65"
                        Plugin._state["audio_fix_error"] = ""
                    return
                await asyncio.to_thread(apply_audio_fix)
                if not await asyncio.to_thread(audio_fix_ready):
                    raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
                with _lock:
                    Plugin._state["audio_fix_supported"] = True
                    Plugin._state["audio_fix_installed"] = True
                    Plugin._state["audio_calibration_available"] = True
                    Plugin._state["audio_profile"] = "AYANEO v0.65"
                    Plugin._state["audio_fix_error"] = ""
                decky.logger.info(f"{LOG} loaded AYANEO CS35L41 speaker tuning")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with _lock:
                    Plugin._state["audio_calibration_available"] = False
                    Plugin._state["audio_fix_error"] = str(error)
                decky.logger.warning(f"{LOG} audio tuning attempt failed: {error}")
        decky.logger.error(f"{LOG} could not load AYANEO speaker tuning")

    async def _ac_loop(self):
        """Restore the active TDP after firmware reacts to charger changes."""
        previous = await asyncio.to_thread(ac_online)
        while True:
            await asyncio.sleep(1)
            current = await asyncio.to_thread(ac_online)
            if current == previous:
                continue
            previous = current
            decky.logger.info(f"{LOG} charger {'connected' if current else 'disconnected'}; restoring TDP")
            # Firmware profile writes can land after the power-supply event, so
            # one immediate write is not enough. These intervals total 6 s.
            for delay in (0.5, 1.0, 1.5, 3.0):
                await asyncio.sleep(delay)
                with _lock:
                    target = dict(Plugin._state["tdp"])
                try:
                    await asyncio.to_thread(apply_tdp, target)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    decky.logger.warning(f"{LOG} charger TDP restore failed: {error}")

    async def _module_loop(self):
        """Track module identity and restore the controller after a module swap."""
        previous_connected = None
        restore_pending = False
        identify_pending = True
        while True:
            await asyncio.sleep(0.5)
            try:
                powered = await asyncio.to_thread(controller_powered)
                presence = await asyncio.to_thread(module_presence)
                connected = all(presence.values())
                reconnected = previous_connected is False and connected
                previous_connected = connected
                with _lock:
                    Plugin._state["modules_connected"] = connected
                if not connected:
                    left, right = module_states_from_presence(presence)
                    with _lock:
                        Plugin._state["module_left"] = left
                        Plugin._state["module_right"] = right
                    identify_pending = True
                    continue
                if not powered:
                    await asyncio.to_thread(set_controller_power, True)
                    restore_pending = True
                elif reconnected:
                    # A manually removed module loses its LED state while the
                    # base controller remains powered, so power state alone is
                    # not enough to detect that RGB needs to be sent again.
                    restore_pending = True
                if restore_pending:
                    with _lock:
                        Plugin._state["module_left"] = _module_info("left", status="activating")
                        Plugin._state["module_right"] = _module_info("right", status="activating")
                with _lock:
                    controller = dict(Plugin._state["controller"])
                last_error = None
                if restore_pending:
                    for delay in (0.5, 1.0, 2.0, 3.0):
                        await asyncio.sleep(delay)
                        try:
                            if not await asyncio.to_thread(both_modules_connected):
                                break
                            # Rear-button mappings are persisted with AYA_SAVE.
                            # Reprogramming them here would also issue the 0x88
                            # physical module reset on every reconnection.
                            await asyncio.to_thread(apply_controller, controller)
                            await asyncio.to_thread(set_vibration_gain, controller["ff_gain"])
                            restore_pending = False
                            identify_pending = True
                            with _lock:
                                Plugin._state["modules_reconnecting"] = False
                            decky.logger.info(
                                f"{LOG} modules connected; restored RGB, vibration and FF_GAIN")
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            last_error = error
                if restore_pending and last_error:
                    decky.logger.warning(f"{LOG} controller setting restore failed: {last_error}")
                if identify_pending and not restore_pending:
                    try:
                        left, right = await asyncio.to_thread(read_module_layout)
                        with _lock:
                            Plugin._state["module_left"] = left
                            Plugin._state["module_right"] = right
                        identify_pending = False
                        decky.logger.info(
                            f"{LOG} modules detected: left {left['label']} (0x{left['code']:02X}), "
                            f"right {right['label']} (0x{right['code']:02X})")
                    except Exception as error:
                        decky.logger.debug(f"{LOG} module identification pending: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.debug(f"{LOG} module monitor unavailable: {error}")

    async def _tm_guard_loop(self):
        """Return from accidental hardware TM mode changes without touching LC/RC."""
        while True:
            await asyncio.sleep(TM_GUARD_INTERVAL)
            with _lock:
                enabled = Plugin._state.get("tm_guard_enabled", False)
                reconnecting = Plugin._state.get("modules_reconnecting", False)
                controller = dict(Plugin._state["controller"])
                restore_buttons = Plugin._state.get("button_fix_installed", False)
            if not enabled:
                continue
            if reconnecting:
                with _lock:
                    Plugin._state["tm_guard_status"] = "Waiting for modules"
                continue
            try:
                changed = await asyncio.to_thread(recover_tm_mode, controller, restore_buttons)
                with _lock:
                    if changed:
                        Plugin._state["tm_guard_recoveries"] += 1
                        Plugin._state["tm_guard_status"] = "Custom mode restored"
                    else:
                        Plugin._state["tm_guard_status"] = "Monitoring"
                if changed:
                    decky.logger.info(f"{LOG} TM Guard restored custom controller mode")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with _lock:
                    Plugin._state["tm_guard_status"] = "Waiting for controller"
                decky.logger.debug(f"{LOG} TM Guard waiting: {error}")

    async def _main(self):
        await asyncio.to_thread(settings.read)
        is_supported = await asyncio.to_thread(supported_device)
        saved_controller = normalize_controller(settings.getSetting("controller", DEFAULT_CONTROLLER))
        # Treat an existing map as an enabled toggle and migrate it in place
        # when a newer package extends the aya7 mapping.
        key_binding_installed = any(path.exists() for path in (
            INPUT_MAP_TARGET, *LEGACY_INPUT_MAP_TARGETS, LEGACY_INPUT_DEVICE_TARGET))
        if key_binding_installed:
            await asyncio.to_thread(install_button_fix)
        audio_enabled = bool(settings.getSetting("audio_fix_enabled", True))
        tm_guard_enabled = bool(settings.getSetting("tm_guard_enabled", True))
        audio_installed = await asyncio.to_thread(audio_fix_installed)
        audio_ready = await asyncio.to_thread(audio_fix_ready) if audio_installed else False
        charge_control = await asyncio.to_thread(ensure_charge_control) if is_supported else None
        try:
            charge_bypass = await asyncio.to_thread(read_charge_bypass) if charge_control else False
        except (OSError, RuntimeError):
            charge_control = None
            charge_bypass = False
        Plugin._state = {
            "supported": is_supported,
            "device": _dmi("product_name") or "unknown",
            "tdp_backend": await asyncio.to_thread(tdp_backend),
            "tdp": normalize_tdp(settings.getSetting("tdp", DEFAULT_TDP)),
            "presets": PRESETS,
            "controller": saved_controller,
            "screen_installed": _is_our_display_script(LUA_TARGET),
            "edid_patched": False,
            "edid_game_nits": 0,
            "button_fix_installed": key_binding_installed,
            "charge_bypass_supported": charge_control is not None,
            "charge_bypass": charge_bypass,
            "module_eject_supported": charge_control is not None,
            "module_reset_supported": charge_control is not None,
            "modules_reconnecting": False,
            "modules_connected": True,
            "module_left": _module_info("left", status="detecting"),
            "module_right": _module_info("right", status="detecting"),
            "tm_guard_enabled": tm_guard_enabled,
            "tm_guard_status": "Monitoring" if tm_guard_enabled else "Disabled",
            "tm_guard_recoveries": 0,
            "gpu_power_w": None,
            "audio_fix_supported": await asyncio.to_thread(audio_fix_supported),
            "audio_fix_enabled": audio_enabled,
            "audio_fix_installed": audio_installed,
            "audio_calibration_available": audio_enabled and audio_ready,
            "audio_calibration_last": settings.getSetting("audio_calibration_last", ""),
            "audio_profile": "AYANEO v0.65" if audio_ready else
                             ("Pending" if audio_enabled else "Generic fallback"),
            "audio_fix_error": "",
        }
        Plugin._active_app = ""
        if is_supported:
            Plugin._restore_task = asyncio.create_task(self._restore_hardware())
            Plugin._edid_task = asyncio.create_task(self._edid_loop())
            Plugin._ac_task = asyncio.create_task(self._ac_loop())
            Plugin._module_task = asyncio.create_task(self._module_loop())
            Plugin._tm_guard_task = asyncio.create_task(self._tm_guard_loop())
            if audio_enabled:
                Plugin._audio_task = asyncio.create_task(self._restore_audio())
        decky.logger.info(f"{LOG} started on {Plugin._state['device']}")

    async def _unload(self):
        tasks = [task for task in (
            Plugin._audio_task, Plugin._restore_task, Plugin._edid_task,
            Plugin._ac_task, Plugin._module_task, Plugin._tm_guard_task,
        ) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        Plugin._audio_task = None
        Plugin._restore_task = None
        Plugin._edid_task = None
        Plugin._ac_task = None
        Plugin._module_task = None
        Plugin._tm_guard_task = None
        try:
            if not await asyncio.to_thread(controller_powered):
                await asyncio.to_thread(set_controller_power, True)
        except Exception as error:
            decky.logger.warning(f"{LOG} could not restore controller power on unload: {error}")
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        try:
            await asyncio.to_thread(remove_audio_fix, False)
        except Exception as error:
            decky.logger.warning(f"{LOG} could not remove audio firmware path: {error}")
        if settings.getSetting("charge_bypass", False):
            try:
                await asyncio.to_thread(write_charge_bypass, False)
                decky.logger.info(f"{LOG} restored automatic charging before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore charging before uninstall: {error}")
        if _is_our_display_script(LUA_TARGET):
            LUA_TARGET.unlink()
        try:
            await asyncio.to_thread(program_rear_buttons, False)
        except Exception as error:
            decky.logger.warning(f"{LOG} could not clear LC1/RC1 bindings: {error}")
        remove_button_fix()
        await asyncio.to_thread(_systemctl, "restart", "inputplumber")
