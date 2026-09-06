# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/Ayaneo3Companion

"""AYANEO 3 hardware controls for Decky Loader."""

import asyncio
import colorsys
import contextlib
import copy
import glob
import hashlib
import json
import math
import os
import re
import select
import shutil
import ssl
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import urllib.parse
from pathlib import Path

import decky

LOG = "[ayaneo3companion]"
PLUGIN_DIR = Path(decky.DECKY_PLUGIN_DIR)
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

# Decky aliases its own updater module to the bare name ``updater`` before a
# plugin is loaded, so use the collision-proof module name shared by the other
# Rayek plugins.
from lego_updater import Updater  # noqa: E402
from safe_settings import SettingsManager  # noqa: E402
from quality_runtime import complete, guard_public_calls  # noqa: E402

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/Rayekkk/Ayaneo3Companion/releases/latest"
)
def _decky_account():
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


_DECKY_ACCOUNT = _decky_account()
updater = Updater(
    releases_url=GITHUB_RELEASES_URL,
    user_agent="Ayaneo3Companion",
    log_prefix=LOG,
    plugin_dir=str(PLUGIN_DIR),
    asset_name_template="Ayaneo3Companion-{version}.zip",
    logger=decky.logger,
    **_DECKY_ACCOUNT,
)

BIN_DIR = PLUGIN_DIR / "bin"
RYZENADJ = BIN_DIR / "ryzenadj"
RYZENADJ_LIB = BIN_DIR / "libryzenadj.so"
RYZENADJ_URL = "https://github.com/FlyGoat/RyzenAdj/releases/download/v0.19.0/ryzenadj-manylinux_2_28-x86_64.tar.gz"
RYZENADJ_ARCHIVE_SHA256 = "d04547f111c6af3e40d3f210468adb884561618ddade0b640d90e50c88d03444"
RYZENADJ_BINARY_SHA256 = "18a61170efec95d2366355b9dd5c75a961a9e8008d42e3471f4f414a6faec471"
RYZENADJ_LIBRARY_SHA256 = "665a91ee17273e0a8039eacfc91d2753e87148ea9983feb683a54f3161005f87"
CPU_BOOST_PATH = Path("/sys/devices/system/cpu/cpufreq/boost")
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
DISPLAY_SCRIPT_MARKER = b"-- Managed by AYANEO 3 Companion\n"
DISPLAY_SCRIPT_BACKUP_ROOT = Path("/var/lib/ayaneo3-companion/display-definition")
LEGACY_DISPLAY_SCRIPT_SHA256 = frozenset({
    # Initial public Gamma 2.2 definition (Display-P3 primaries).
    "98de33235379f187b9d0eeb1460016b71fbc808c7cecff574774bbae64a130d4",
    # Pre-marker DXQ7D0023 calibration.
    "f52b721078df6336855c543da325a908477382cd8f313d395a7eabf1ad9f0b21",
})
PUBLISHED_EDID = (Path(_DECKY_ACCOUNT["user_home"]) / ".config/gamescope/edid.bin"
                  if _DECKY_ACCOUNT else None)
PUBLISHED_EDID_UID = _DECKY_ACCOUNT.get("user_uid")
EDID_TARGET_NITS = 800
INPUT_MAP_SOURCE = PLUGIN_DIR / "assets" / "ayaneo3-companion.yaml"
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
CONTROLLER_GAIN_CHECK_INTERVAL = 2.0
CONTROLLER_RESUME_THRESHOLD = 1.0
HID_COMMAND_ATTEMPTS = 3
HID_AYANEO_DRIVER_NAMES = frozenset({"hid-ayaneo", "hid_ayaneo"})
CONTROLLER_COLOR_TOLERANCE = 1
AYA3_GAMEPAD_PHYS_PATHS = frozenset({
    # InputPlumber's AYANEO 3 source definitions. The PCI topology differs
    # between the Ryzen 7 8840U and Ryzen AI 9 HX 370 variants.
    "usb-0000:c4:00.3-2/input0",
    "usb-0000:c7:00.0-2/input0",
})

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
_tdp_mutation_lock = threading.RLock()
_cpu_boost_lock = threading.Lock()
_ec_lock = threading.Lock()
_charge_apply_lock = threading.Lock()
_controller_apply_lock = threading.RLock()
_controller_event_lock = threading.Lock()
_audio_apply_lock = threading.Lock()

def _dmi(name: str) -> str:
    try:
        return Path(f"/sys/class/dmi/id/{name}").read_text().strip()
    except OSError:
        return ""


def supported_device() -> bool:
    return _dmi("sys_vendor").upper() == "AYANEO" and _dmi("product_name").upper() == "AYANEO 3"


def require_supported_device(action: str = "hardware control") -> None:
    """Enforce the AYANEO 3 boundary in the privileged backend."""
    if not supported_device():
        raise RuntimeError(f"{action} is restricted to AYANEO 3")


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


def _atomic_write_bytes(target: Path, data: bytes, mode: int = 0o644) -> None:
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
        _atomic_write_bytes(target, source.read_bytes())
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
    account = _decky_account()
    uid = account.get("user_uid", -1)
    if type(uid) is not int or uid <= 0:
        raise RuntimeError("the Decky user account is unavailable for audio control")
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError) as error:
        raise RuntimeError("the Decky user account is unavailable for audio control") from error
    runtime = f"/run/user/{uid}"
    return subprocess.run([
        "/usr/bin/runuser", "-u", name, "--", "/usr/bin/env",
        f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        *arguments,
    ], capture_output=True, text=True, timeout=timeout, env=_clean_subprocess_env())


def _suspend_audio_outputs() -> list[str]:
    try:
        result = _deck_audio_command(["/usr/bin/pactl", "list", "short", "sinks"])
    except Exception as error:
        decky.logger.warning(f"{LOG} could not list PipeWire sinks: {error}")
        return []
    if result.returncode:
        decky.logger.warning(f"{LOG} could not list PipeWire sinks: {result.stderr.strip()}")
        return []
    suspended = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        name = fields[1]
        try:
            changed = _deck_audio_command(["/usr/bin/pactl", "suspend-sink", name, "1"])
        except Exception as error:
            decky.logger.warning(f"{LOG} could not suspend sink {name}: {error}")
            continue
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
        # A failed PipeWire restart must never skip the best-effort sink resume.
        # The restart exception still propagates after this inner finally.
        try:
            if services_stopped:
                _set_audio_services(True)
        finally:
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
    card = _audio_card_index()
    if card is None:
        raise RuntimeError("AYANEO CS35L41 audio controls were not found")
    try:
        AUDIO_FIRMWARE_PATH.write_text(str(AUDIO_FIRMWARE_ROOT) + "\n")
        if AUDIO_FIRMWARE_PATH.read_text().strip() != str(AUDIO_FIRMWARE_ROOT):
            raise RuntimeError("kernel did not retain the audio firmware path")
        _reload_audio_dsps(card)
    except Exception as error:
        try:
            # A zero-byte write does not invoke a sysfs parameter setter.
            # Send a newline when restoring the kernel's empty default path.
            AUDIO_FIRMWARE_PATH.write_text(configured + "\n")
            if AUDIO_FIRMWARE_PATH.read_text().strip() != configured:
                raise RuntimeError("audio firmware path rollback readback mismatch")
            _reload_audio_dsps(card)
        except Exception as recovery:
            raise RuntimeError(f"{error}; audio recovery failed: {recovery}") from error
        raise


def apply_audio_fix() -> None:
    require_supported_device("audio tuning")
    with _audio_apply_lock:
        _apply_audio_fix_locked()


def _remove_audio_fix_locked(reload_dsp: bool = True) -> None:
    try:
        configured = AUDIO_FIRMWARE_PATH.read_text().strip()
    except OSError as error:
        raise RuntimeError("cannot safely read the kernel audio firmware path") from error
    changed = configured == str(AUDIO_FIRMWARE_ROOT)
    try:
        if changed:
            AUDIO_FIRMWARE_PATH.write_text("\n")
            if AUDIO_FIRMWARE_PATH.read_text().strip():
                raise RuntimeError("kernel did not clear the audio firmware path")
        if reload_dsp and changed:
            card = _audio_card_index()
            if card is None:
                raise RuntimeError("AYANEO CS35L41 audio controls were not found")
            _reload_audio_dsps(card)
    except Exception as error:
        if changed:
            try:
                AUDIO_FIRMWARE_PATH.write_text(str(AUDIO_FIRMWARE_ROOT) + "\n")
                if AUDIO_FIRMWARE_PATH.read_text().strip() != str(AUDIO_FIRMWARE_ROOT):
                    raise RuntimeError("audio firmware path rollback readback mismatch")
                if reload_dsp:
                    card = _audio_card_index()
                    if card is not None:
                        _reload_audio_dsps(card)
            except Exception as recovery:
                raise RuntimeError(f"{error}; audio recovery failed: {recovery}") from error
        raise
    if AUDIO_FIRMWARE_ROOT.exists():
        shutil.rmtree(AUDIO_FIRMWARE_ROOT)


def remove_audio_fix(reload_dsp: bool = True) -> None:
    require_supported_device("audio tuning")
    with _audio_apply_lock:
        _remove_audio_fix_locked(reload_dsp)


def _download_audio_calibration() -> bytes:
    request = urllib.request.Request(
        _checked_download_url(AUDIO_CALIBRATION_URL),
        headers={"User-Agent": f"Ayaneo3Companion/{updater.plugin_version()}"},
    )
    with _open_hardware_download(request) as response:
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
        _atomic_write_bytes(wmfw_target, wmfw_source.read_bytes())

    bin_target = target_dir / AUDIO_CALIBRATION_BIN
    try:
        valid_bin = hashlib.sha256(bin_target.read_bytes()).hexdigest() == AUDIO_CALIBRATION_SHA256
    except OSError:
        valid_bin = False
    if not valid_bin:
        data = _download_audio_calibration()
        _atomic_write_bytes(bin_target, data)
    return wmfw_target, bin_target


def _set_audio_firmware_type(card: int, profile: int) -> None:
    disabled = []
    try:
        for control in AUDIO_FIRMWARE_CONTROLS:
            _set_audio_firmware_load(card, control, False)
            disabled.append(control)
        time.sleep(0.3)
        for control in AUDIO_FIRMWARE_TYPE_CONTROLS:
            _set_audio_control(card, control, profile, "select firmware for")
        for control in tuple(disabled):
            _set_audio_firmware_load(card, control, True)
            disabled.remove(control)
        time.sleep(1.0)
    finally:
        # Calibration changes both amplifiers as one operation. If any control
        # fails halfway through, make a final attempt to leave every DSP loaded.
        for control in disabled:
            try:
                _set_audio_firmware_load(card, control, True)
            except Exception as error:
                decky.logger.error(f"{LOG} could not recover {control}: {error}")


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
    rollback_error = None
    lock_error = None
    try:
        try:
            write_blob(candidate)
            if path.read_bytes() != candidate:
                raise RuntimeError("audio calibration EFI readback mismatch")
        except Exception as error:
            write_error = error
            try:
                try:
                    current = path.read_bytes()
                except OSError:
                    current = None
                if current != original:
                    write_blob(original)
                if path.read_bytes() != original:
                    raise RuntimeError("audio calibration rollback readback mismatch")
            except Exception as recovery:
                rollback_error = recovery
    finally:
        try:
            locked = subprocess.run(["/usr/bin/chattr", "+i", str(path)],
                                    capture_output=True, text=True, timeout=10,
                                    env=_clean_subprocess_env())
            if locked.returncode:
                lock_error = RuntimeError(locked.stderr.strip() or "chattr failed")
        except Exception as error:
            lock_error = error
    failures = []
    if write_error is not None:
        failures.append(f"could not save audio calibration: {write_error}")
    if rollback_error is not None:
        failures.append(f"audio calibration recovery failed: {rollback_error}")
    if lock_error is not None:
        failures.append(f"could not relock audio calibration EFI: {lock_error}")
    if failures:
        raise RuntimeError("; ".join(failures)) from (write_error or lock_error)


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
        try:
            if playback.poll() is None:
                playback.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    playback.wait(timeout=2)
            if playback.poll() is None:
                playback.kill()
                playback.wait(timeout=2)
        finally:
            if playback.stderr is not None:
                playback.stderr.close()


def perform_audio_recalibration() -> dict:
    require_supported_device("audio recalibration")
    with _audio_apply_lock:
        if not audio_fix_ready():
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


def _charge_behaviour_path() -> Path | None:
    """Return the kernel charge-control ABI when ayaneo-ec provides it."""
    try:
        candidates = sorted(POWER_SUPPLY_ROOT.iterdir())
    except OSError:
        return None
    for candidate in candidates:
        try:
            if (candidate / "type").read_text().strip() != "Battery":
                continue
            path = candidate / "charge_behaviour"
            if path.exists():
                return path
        except OSError:
            continue
    return None


def _read_charge_behaviour(path: Path) -> bool:
    value = path.read_text().strip()
    if "[" in value and "]" in value:
        value = value.split("[", 1)[1].split("]", 1)[0].strip()
    if value not in ("auto", "inhibit-charge"):
        raise RuntimeError(f"unexpected charge_behaviour value: {value or 'empty'}")
    return value == "inhibit-charge"


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
                                capture_output=True, text=True, timeout=10,
                                env=_clean_subprocess_env())
        if result.returncode:
            decky.logger.warning(f"{LOG} cannot load ec_sys: {result.stderr.strip()}")
            return None
        path = _ec_io_path()
        try:
            writable = write_flag.read_text().strip().lower() in ("y", "1")
        except OSError:
            writable = False
    return path if writable else None


def ensure_charge_bypass_control() -> Path | None:
    """Select the upstream charge ABI or the signed raw-EC fallback."""
    behaviour = _charge_behaviour_path()
    if behaviour is not None:
        try:
            _read_charge_behaviour(behaviour)
            return behaviour
        except (OSError, RuntimeError) as error:
            decky.logger.warning(f"{LOG} cannot use charge_behaviour: {error}")
            return None
    return ensure_charge_control()


def read_charge_bypass() -> bool:
    behaviour = _charge_behaviour_path()
    if behaviour is not None:
        return _read_charge_behaviour(behaviour)

    path = _ec_io_path()
    if path is None:
        raise RuntimeError("AYANEO EC access is unavailable")
    with _ec_lock, path.open("rb", buffering=0) as ec:
        ec.seek(EC_CHARGE_REGISTER)
        value = ec.read(1)
    if len(value) != 1:
        raise RuntimeError("could not read AYANEO charge register")
    # This matches the upstream ayaneo-ec driver: only 0x55 means inhibit;
    # firmware defaults such as 0x00 are ordinary automatic charging until a
    # userspace tool explicitly writes 0xaa or 0x55.
    return value[0] == EC_CHARGE_INHIBIT


def write_charge_bypass(enabled: bool) -> None:
    with _charge_apply_lock:
        _write_charge_bypass_unlocked(enabled)


def _write_charge_bypass_unlocked(enabled: bool) -> None:
    if not supported_device():
        raise RuntimeError("charge bypass is restricted to AYANEO 3")
    path = ensure_charge_bypass_control()
    if path is None:
        raise RuntimeError("AYANEO EC charge control is unavailable")

    behaviour = _charge_behaviour_path()
    if behaviour is not None:
        previous = _read_charge_behaviour(behaviour)
        try:
            behaviour.write_text("inhibit-charge\n" if enabled else "auto\n")
            if _read_charge_behaviour(behaviour) != enabled:
                raise RuntimeError("kernel charge control did not retain the setting")
        except Exception as error:
            try:
                behaviour.write_text("inhibit-charge\n" if previous else "auto\n")
                if _read_charge_behaviour(behaviour) != previous:
                    raise RuntimeError("charge control rollback readback mismatch")
            except Exception as recovery:
                raise RuntimeError(f"{error}; charge recovery failed: {recovery}") from error
            raise
        return

    value = EC_CHARGE_INHIBIT if enabled else EC_CHARGE_AUTO
    with _ec_lock, path.open("r+b", buffering=0) as ec:
        ec.seek(EC_CHARGE_REGISTER)
        previous = ec.read(1)
        if len(previous) != 1:
            raise RuntimeError("could not read AYANEO charge register")
        try:
            ec.seek(EC_CHARGE_REGISTER)
            if ec.write(bytes([value])) != 1:
                raise RuntimeError("AYANEO EC rejected the charge setting")
            ec.seek(EC_CHARGE_REGISTER)
            if ec.read(1) != bytes([value]):
                raise RuntimeError("AYANEO EC did not retain the charge setting")
        except Exception as error:
            try:
                ec.seek(EC_CHARGE_REGISTER)
                if ec.write(previous) != 1:
                    raise RuntimeError("short EC charge rollback write")
                ec.seek(EC_CHARGE_REGISTER)
                if ec.read(1) != previous:
                    raise RuntimeError("EC charge rollback readback mismatch")
            except Exception as recovery:
                raise RuntimeError(f"{error}; charge recovery failed: {recovery}") from error
            raise


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
    require_supported_device("embedded-controller writes")
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


def tdp_preset(raw, stored=None) -> str:
    """Keep the explicit UI choice, falling back to value-based migration."""
    if stored in (*PRESETS.keys(), "Custom"):
        return stored
    value = normalize_tdp(raw)
    for name, limits in PRESETS.items():
        if value == limits:
            return name
    return "Custom"


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


def _hid_driver_name(device_path: Path) -> str:
    try:
        return (device_path / "driver").resolve(strict=True).name.lower()
    except OSError:
        return ""


def _kernel_hid_ayaneo_active(device_path: Path) -> bool:
    """Detect the kernel driver before issuing unsynchronized raw commands."""
    if _hid_driver_name(device_path) in HID_AYANEO_DRIVER_NAMES:
        return True
    # Keep detection compatible with development versions whose driver name may
    # still change while retaining the proposed sysfs ABI.
    return all((device_path / name).exists()
               for name in ("module_left", "module_right", "eject", "reset"))


def _vendor_hidraw() -> str:
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        device_path = Path(sys_path).resolve() / "device"
        try:
            descriptor = (device_path / "report_descriptor").read_bytes()
            uevent = (device_path / "uevent").read_text()
        except OSError:
            continue
        if descriptor.startswith(b"\x06\x00\xff\x09\x01") and "HID_ID=0003:00001C4F:00000002" in uevent:
            if _kernel_hid_ayaneo_active(device_path):
                raise RuntimeError(
                    "kernel hid-ayaneo is active; direct controller commands are disabled "
                    "to prevent conflicting HID transactions"
                )
            return "/dev/" + Path(sys_path).name
    raise RuntimeError("AYANEO vendor HID interface not found")


def _hid_exchange(fd, command: bytes, timeout=0.4) -> bytes:
    """Send a command and retry the complete transaction if it is unanswered."""
    for _ in range(HID_COMMAND_ATTEMPTS):
        if os.write(fd, command) != len(command):
            continue
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            response = os.read(fd, 64)
            if len(response) == 64 and response[3] == command[4]:
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
    with _controller_apply_lock:
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
    require_supported_device("controller configuration")
    config = normalize_controller(config)
    feedback_level = config["vibration"]
    with _controller_apply_lock:
        # Once Off is applied the firmware ignores force-feedback, so confirm
        # that transition with the previous level immediately beforehand.
        if (vibration_feedback and feedback_level == "off"
                and previous_vibration in ("low", "medium", "high")):
            try:
                play_vibration_test(previous_vibration, VIBRATION_CONFIRM_MS)
            except Exception as error:
                # Confirmation is cosmetic. It must never prevent the actual
                # firmware setting (especially Off) from being applied.
                decky.logger.warning(f"{LOG} vibration confirmation skipped: {error}")
        path = _vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            command = controller_command(config)
            if not _hid_exchange(fd, command):
                raise RuntimeError("controller rejected configuration")
            verification = _hid_exchange(fd, AYA_CHECK)
            if (verification and verification[3] == AYA_CHECK[4]
                    and not controller_response_matches(verification, config)):
                if not _hid_exchange(fd, command):
                    raise RuntimeError("controller rejected configuration retry")
                verification = _hid_exchange(fd, AYA_CHECK)
                if (verification and verification[3] == AYA_CHECK[4]
                        and not controller_response_matches(verification, config)):
                    raise RuntimeError("controller did not retain configuration")
            # The configuration command applies the new level immediately.
            # Confirm it before the slower non-volatile save operation.
            if vibration_feedback and feedback_level != "off":
                try:
                    play_vibration_test(feedback_level, VIBRATION_CONFIRM_MS)
                except Exception as error:
                    decky.logger.warning(f"{LOG} vibration confirmation skipped: {error}")
            # The plugin persists every setting itself. Firmware save is useful
            # for ordinary RGB/config writes, but it adds a second, much longer
            # controller-side confirmation when changing vibration strength.
            if persist_firmware:
                if not _hid_exchange(fd, AYA_SAVE, timeout=1.5):
                    decky.logger.warning(
                        f"{LOG} controller configuration applied but firmware save was unanswered")
        finally:
            os.close(fd)


def controller_response_matches(response: bytes, config: dict) -> bool:
    """Compare AYA_CHECK's echoed volatile settings with the requested state."""
    if len(response) < 24 or response[3] != AYA_CHECK[4]:
        return False
    config = normalize_controller(config)
    expected = controller_command(config)
    if response[21:23] != expected[22:24] or response[23] != expected[24]:
        return False
    if response[7] != expected[8] or response[11] != expected[12]:
        return False
    mode = config["rgb_mode"]
    if mode not in ("solid", "pulse"):
        return True
    echoed = response[8:11] + response[12:15]
    requested = expected[9:12] + expected[13:16]
    return all(abs(current - target) <= CONTROLLER_COLOR_TOLERANCE
               for current, target in zip(echoed, requested))


def reconcile_controller(config: dict, recover_custom: bool,
                         force: bool = False) -> tuple[str, str]:
    """Verify volatile controller state and repair it without writing NVRAM."""
    require_supported_device("controller monitoring")
    config = normalize_controller(config)
    path = _vendor_hidraw()
    with _controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            response = _hid_exchange(fd, AYA_CHECK)
            if not response:
                raise RuntimeError("controller did not answer the state check")
            if controller_requires_custom(response):
                if not recover_custom:
                    return "tm_mode", path
                _switch_to_custom_mode_fd(fd, response)
                status = "mode_restored"
            elif force or not controller_response_matches(response, config):
                status = "configuration_restored"
            else:
                return "healthy", path
            if not _hid_exchange(fd, controller_command(config)):
                raise RuntimeError("controller rejected automatic configuration restore")
        finally:
            os.close(fd)
    # FF_GAIN belongs to the evdev node and is reset independently when that
    # node is recreated, so restore it together with the firmware packet.
    set_vibration_gain(config["ff_gain"])
    return status, path


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
            completed = False
            for _ in range(20):
                time.sleep(0.4)
                response = _hid_exchange(fd, AYA_CHECK)
                if len(response) > 19 and response[19] & ~0x11 == 0:
                    completed = True
                    break
            if not completed:
                raise TimeoutError(
                    "Magic Module eject timed out; controller power was left on"
                )
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
    require_supported_device("TM Guard")
    if not both_modules_connected() or not controller_powered():
        return False
    status, _ = reconcile_controller(config, True)
    return status == "mode_restored"


def _event_has_rumble(node: str) -> bool:
    try:
        import fcntl
        with open(node, "rb") as handle:
            bits = bytearray(32)
            fcntl.ioctl(handle.fileno(), EVIOCGBIT_FF, bits)
            return bool(bits[FF_RUMBLE // 8] & (1 << (FF_RUMBLE % 8)))
    except (ImportError, OSError):
        return False


def _input_event_identity(node: str) -> tuple[str, str, str]:
    """Return the kernel vendor, product and physical path for an event node."""
    device = Path("/sys/class/input") / Path(node).name / "device"

    def read(name: str) -> str:
        try:
            return (device / name).read_text().strip().lower()
        except OSError:
            return ""

    return read("id/vendor"), read("id/product"), read("phys")


def _rumble_event_node() -> str | None:
    """Return AYANEO's physical gamepad without selecting an external pad."""
    nodes = sorted(glob.glob("/dev/input/event*"),
                   key=lambda path: int("".join(filter(str.isdigit, Path(path).name)) or 0))
    candidates = []
    for node in nodes:
        if not _event_has_rumble(node):
            continue
        vendor, product, phys = _input_event_identity(node)
        # The controller firmware exposes an Xbox 360-compatible xpad node
        # (045e:028e); development firmware can retain AYANEO's 1c4f VID.
        # Never select InputPlumber's 28de virtual controller, because FF_GAIN
        # must scale the physical device itself.
        if not ((vendor == "045e" and product == "028e") or vendor == "1c4f"):
            continue
        if phys in AYA3_GAMEPAD_PHYS_PATHS:
            return node
        candidates.append(node)
    # Older or future kernels may expose a different physical path. A single
    # hardware candidate is still safe; multiple candidates are ambiguous and
    # must not make Companion modify an attached Xbox-compatible controller.
    return candidates[0] if len(candidates) == 1 else None


def _device_node_token(path: str) -> tuple[str, int | None, int | None]:
    """Identify a device-node incarnation even when Linux reuses its name."""
    try:
        info = os.stat(path)
        return path, info.st_rdev, info.st_ino
    except OSError:
        return path, None, None


def _rumble_event_token() -> tuple[str, int, int] | None:
    node = _rumble_event_node()
    if not node:
        return None
    try:
        info = os.stat(node)
        return node, info.st_rdev, info.st_ino
    except OSError:
        return None


def _suspend_clock_offset() -> float | None:
    """Return a value that increases only while Linux is suspended."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


def set_vibration_gain(percent: int) -> None:
    """Set Linux FF_GAIN on AYANEO's physical gamepad input device."""
    require_supported_device("vibration control")
    gain = _clamp(percent, 0, 100)
    with _controller_event_lock:
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
    require_supported_device("vibration control")
    import fcntl
    import time

    # Keep confirmation levels perceptually distinct. Small rumble motors are
    # strongly non-linear, so evenly spaced numeric values feel too similar.
    strength = {"low": 0.20, "medium": 0.55, "high": 1.0}.get(level, 0.0)
    if strength <= 0:
        raise RuntimeError("Vibration is Off - select a strength first")
    duration = max(100, min(2000, int(duration_ms)))
    magnitude = round(0xFFFF * strength)
    with _controller_event_lock:
        node = _rumble_event_node()
        if not node:
            raise RuntimeError("No rumble-capable input device found")
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

            try:
                start = event(1)
                if os.write(fd, start) != len(start):
                    raise RuntimeError("Controller rejected the vibration start event")
                time.sleep(duration / 1000)
            finally:
                # Even an interrupted test or failed evdev write must release
                # the uploaded effect before its descriptor is closed.
                try:
                    stop = event(0)
                    if os.write(fd, stop) != len(stop):
                        raise RuntimeError("Controller rejected the vibration stop event")
                finally:
                    fcntl.ioctl(fd, EVIOCRMFF, effect_id)
        finally:
            os.close(fd)


def _powerstation_card():
    if subprocess.run(["busctl", "--system", "status", "org.shadowblip.PowerStation"],
                      capture_output=True, timeout=5,
                      env=_clean_subprocess_env()).returncode != 0:
        return None
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        path = f"/org/shadowblip/Performance/GPU/{Path(card).name}"
        probe = subprocess.run(["busctl", "--system", "get-property", "org.shadowblip.PowerStation",
                                path, "org.shadowblip.GPU.Card.TDP", "TDP"], capture_output=True,
                               timeout=5, env=_clean_subprocess_env())
        if probe.returncode == 0:
            return path
    return None


def _read_powerstation_property(card: str, prop: str) -> float:
    result = subprocess.run([
        "busctl", "--system", "get-property", "org.shadowblip.PowerStation",
        card, "org.shadowblip.GPU.Card.TDP", prop,
    ], capture_output=True, text=True, timeout=10, env=_clean_subprocess_env())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not read PowerStation {prop}")
    fields = result.stdout.split()
    try:
        if len(fields) != 2 or fields[0] != "d":
            raise ValueError("unexpected property type")
        value = float(fields[1])
        if not math.isfinite(value) or value < 0:
            raise ValueError("invalid power value")
        return value
    except ValueError as error:
        raise RuntimeError(f"invalid PowerStation {prop} response") from error


def _write_powerstation_property(card: str, prop: str, value: float) -> None:
    result = subprocess.run([
        "busctl", "--system", "set-property", "org.shadowblip.PowerStation",
        card, "org.shadowblip.GPU.Card.TDP", prop, "d", str(float(value)),
    ], capture_output=True, text=True, timeout=10, env=_clean_subprocess_env())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not set PowerStation {prop}")


def _checked_download_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise RuntimeError("refusing untrusted RyzenAdj download URL")
    return url


class _HardwareDownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Validate before following a redirect; checking only the final URL
        # would still send a privileged request to an untrusted destination.
        _checked_download_url(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _open_hardware_download(request):
    opener = urllib.request.build_opener(
        _HardwareDownloadRedirect(), urllib.request.HTTPSHandler(context=_ssl_context()))
    return opener.open(request, timeout=30)


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
        headers={"User-Agent": f"Ayaneo3Companion/{updater.plugin_version()}"},
    )
    with _open_hardware_download(request) as response:
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
            _atomic_write_bytes(target, data, mode)


def tdp_backend() -> str:
    return "PowerStation" if _powerstation_card() else "RyzenAdj"


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
    require_supported_device("CPU Boost")
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


def _apply_tdp_unlocked(config: dict) -> None:
    require_supported_device("TDP control")
    values = normalize_tdp(config)
    card = _powerstation_card()
    if card:
        # PowerStation exposes sustained TDP plus boost headroom. Its backend
        # derives fast PPT, so keep the user-facing three-limit relationship.
        boost = max(0, values["sppt"] - values["spl"])
        wanted = {"TDP": float(values["spl"]), "Boost": float(boost)}
        previous = {prop: _read_powerstation_property(card, prop) for prop in wanted}
        try:
            for prop, value in wanted.items():
                _write_powerstation_property(card, prop, value)
            for prop, value in wanted.items():
                if not math.isclose(_read_powerstation_property(card, prop), value, abs_tol=0.01):
                    raise RuntimeError(f"PowerStation did not retain {prop}")
        except Exception as error:
            failures = []
            for prop, value in previous.items():
                try:
                    _write_powerstation_property(card, prop, value)
                    if not math.isclose(_read_powerstation_property(card, prop), value, abs_tol=0.01):
                        raise RuntimeError("rollback readback mismatch")
                except Exception as recovery:
                    failures.append(f"{prop}: {recovery}")
            if failures:
                raise RuntimeError(f"{error}; PowerStation recovery failed: {'; '.join(failures)}") from error
            raise
        return
    _ensure_ryzenadj()
    env = _clean_subprocess_env()
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


def _normalized_file_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes().replace(b"\r\n", b"\n")
    except OSError:
        return None


def _display_script_owned(path: Path) -> bool:
    data = _normalized_file_bytes(path)
    if data is None:
        return False
    return (data.startswith(DISPLAY_SCRIPT_MARKER) or
            hashlib.sha256(data).hexdigest() in LEGACY_DISPLAY_SCRIPT_SHA256)


def _is_our_display_script(path: Path) -> bool:
    data = _normalized_file_bytes(path)
    source = _normalized_file_bytes(LUA_SOURCE)
    return data is not None and source is not None and data == source


def _display_script_conflict(path: Path) -> bool:
    return path.is_symlink() or (path.exists() and not _display_script_owned(path))


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
    require_supported_device("display definition")
    if LUA_TARGET.is_symlink():
        raise RuntimeError("refusing to replace a symlinked gamescope definition")
    backup = None
    if LUA_TARGET.exists() and not _display_script_owned(LUA_TARGET):
        if not replace_existing:
            raise RuntimeError("display definition replacement requires confirmation")
        backup = _backup_display_script(LUA_TARGET.read_bytes())
    LUA_TARGET.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(LUA_TARGET, LUA_SOURCE.read_bytes())
    return backup


def remove_display_script() -> None:
    """Remove only the display definition owned by this plugin."""
    require_supported_device("display definition")
    if LUA_TARGET.is_symlink():
        raise RuntimeError("refusing to remove a symlinked gamescope definition")
    if not LUA_TARGET.exists():
        return
    if not _display_script_owned(LUA_TARGET):
        raise RuntimeError("another AYANEO 3 gamescope definition is installed")
    LUA_TARGET.unlink()


def _cta_luminance_code(nits: float) -> int:
    return max(1, min(255, round(32 * math.log2(float(nits) / 50))))


def patch_ayaneo_edid(data: bytes, nits: float = EDID_TARGET_NITS):
    """Return an AYANEO 3 EDID with CTA MaxCLL set to the advertised peak."""
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
        if not 4 <= dtd_start <= 127:
            return None
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


def button_map_bytes() -> bytes:
    """Return the aya7 extension with both native and legacy QAM inputs."""
    return INPUT_MAP_SOURCE.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")


def button_map_owned(path: Path) -> bool:
    """Recognise current, marked and known legacy Companion maps."""
    if path in (LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS):
        return path.exists()
    data = _normalized_file_bytes(path)
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
    require_supported_device("key-binding setup")
    if INPUT_MAP_TARGET.is_symlink():
        raise RuntimeError("refusing to replace a symlinked InputPlumber map")
    if INPUT_MAP_TARGET.exists() and not button_map_owned(INPUT_MAP_TARGET):
        raise RuntimeError("another aya7 InputPlumber override already exists")
    INPUT_MAP_TARGET.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(INPUT_MAP_TARGET, button_map_bytes())
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


@guard_public_calls
class Plugin:
    _state = {}
    _restore_task = None
    _edid_task = None
    _ac_task = None
    _module_task = None
    _tm_guard_task = None
    _audio_task = None
    _active_app = ""

    def __init__(self):
        self._closing = False
        self._initializing = False
        self._rpc_lock = asyncio.Lock()
        self._rpc_owner = None
        self._rpc_jobs = set()
        self._workers = set()
        self._audio_mutation_lock = threading.RLock()
        self._startup_task = None
        self._cleanup_task = None

    def _check_write_allowed(self):
        error = getattr(settings, "recovery_error", "")
        if error:
            raise RuntimeError(f"Settings need recovery: {error}")
        if self._initializing:
            raise RuntimeError("Plugin is still starting; try again shortly")
        if Plugin._state.get("startup_error"):
            raise RuntimeError(Plugin._state["startup_error"])
        if Plugin._state.get("supported") is False:
            raise RuntimeError("This device is not an AYANEO 3")

    async def _offload(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        return await complete(task)

    @staticmethod
    def _boolean(value):
        if type(value) is not bool:
            raise ValueError("Expected a boolean value")
        return value

    @staticmethod
    def _app_id(value):
        if value is None or value == "":
            return ""
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
            raise ValueError("Invalid game ID")
        return value

    @classmethod
    def _check_game(cls, expected_app_id):
        if expected_app_id is not None and cls._app_id(expected_app_id) != cls._active_app:
            raise RuntimeError("game is no longer active")

    @staticmethod
    def _save_values(values):
        previous = copy.deepcopy(settings.data)
        try:
            for key, value in values.items():
                settings.setSetting(key, value)
            settings.commit()
        except Exception:
            settings.data = previous
            raise

    @staticmethod
    def _apply_and_save(apply, restore, values):
        try:
            apply()
            Plugin._save_values(values)
        except Exception as error:
            try:
                restore()
            except Exception as rollback_error:
                raise RuntimeError(f"{error}; hardware rollback failed: {rollback_error}") from error
            raise RuntimeError(f"{error}; previous hardware settings restored") from error

    @classmethod
    def _apply_tdp_target(cls, target):
        apply_tdp(target)
        boost = settings.getSetting("cpu_boost", None)
        if type(boost) is bool:
            write_cpu_boost(boost)

    @classmethod
    def _tdp_rollback(cls, target):
        try:
            boost = read_cpu_boost()
        except (OSError, RuntimeError):
            boost = None
        def restore():
            try:
                cls._apply_tdp_target(target)
            finally:
                if type(boost) is bool:
                    write_cpu_boost(boost)
        return restore

    @staticmethod
    def _save(key, value):
        Plugin._save_values({key: value})

    @classmethod
    def _reapply_current_tdp(cls):
        """Apply the newest committed TDP without racing an RPC or app change."""
        with _tdp_mutation_lock:
            with _lock:
                target = dict(cls._state["tdp"])
            cls._apply_tdp_target(target)
            return target

    @classmethod
    def _reconcile_current_controller(cls, recover_custom: bool,
                                      force: bool = False):
        """Read the newest desired state after all earlier writes complete."""
        with _controller_apply_lock:
            with _lock:
                if cls._state.get("modules_reconnecting"):
                    raise RuntimeError("Controller module operation is in progress")
                controller = dict(cls._state["controller"])
                recover_custom = cls._state.get("tm_guard_enabled", recover_custom)
            status, path = reconcile_controller(controller, recover_custom, force)
            return status, path

    @classmethod
    def _restore_current_controller(cls, persist_firmware: bool = False):
        """Atomically snapshot and restore all controller-side settings."""
        with _controller_apply_lock:
            with _lock:
                if (cls._state.get("modules_reconnecting")
                        and not cls._state.get("modules_detached")):
                    raise RuntimeError("Waiting for the requested module operation")
                controller = dict(cls._state["controller"])
            apply_controller(controller, persist_firmware=persist_firmware)
            set_vibration_gain(controller["ff_gain"])
            return controller

    @classmethod
    def _restore_current_vibration_gain(cls):
        with _controller_apply_lock:
            with _lock:
                gain = cls._state["controller"]["ff_gain"]
            set_vibration_gain(gain)
            return gain

    @classmethod
    def _snapshot(cls):
        with _lock:
            state = dict(cls._state)
            state["tdp"] = dict(state.get("tdp", DEFAULT_TDP))
            state["controller"] = dict(state.get("controller", DEFAULT_CONTROLLER))
            state["module_left"] = dict(state.get("module_left", _module_info("left", status="detecting")))
            state["module_right"] = dict(state.get("module_right", _module_info("right", status="detecting")))
        state["settings_error"] = getattr(settings, "recovery_error", "")
        if state["settings_error"] or state.get("initializing") or state.get("startup_error"):
            return state
        if not state.get("supported", False):
            # Never probe AYANEO-specific EC or HID registers merely because a
            # privileged Decky RPC was called on another machine.
            state["gpu_power_w"] = None
            state["cpu_boost_supported"] = False
            state["cpu_boost"] = False
            state["charge_bypass_supported"] = False
            state["module_eject_supported"] = False
            state["module_reset_supported"] = False
            return state
        # Hardware reads can involve sysfs, debugfs and subprocesses. Keep them
        # outside the shared state lock so the monitor loops and RPC writes do
        # not stall behind the QAM's periodic refresh.
        state["gpu_power_w"] = gpu_power_watts()
        try:
            state["cpu_boost"] = read_cpu_boost()
            state["cpu_boost_supported"] = True
        except RuntimeError:
            state["cpu_boost_supported"] = False
        state["screen_installed"] = _is_our_display_script(LUA_TARGET)
        state["screen_conflict"] = _display_script_conflict(LUA_TARGET)
        try:
            edid = read_published_edid()
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
        return await self._offload(self._snapshot)

    async def get_battery_status(self):
        return await self._offload(battery_status)

    async def get_version(self):
        return {"version": updater.plugin_version()}

    async def check_for_updates(self):
        return await self._offload(updater.check)

    async def perform_update(self):
        return await self._offload(updater.download_latest)

    async def set_tdp(self, raw, preset=None, expected_app_id=None):
        value = normalize_tdp(raw)
        profile_name = tdp_preset(value, preset)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                previous = dict(Plugin._state["tdp"])
                self._apply_and_save(
                    lambda: self._apply_tdp_target(value),
                    self._tdp_rollback(previous),
                    {"tdp": value, "tdp_preset": profile_name})
                with _lock:
                    Plugin._state["tdp"] = value
                    Plugin._state["tdp_preset"] = profile_name

        await self._offload(mutate)
        return await self.get_state()

    async def set_cpu_boost(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with _tdp_mutation_lock:
                previous = read_cpu_boost()
                self._apply_and_save(lambda: write_cpu_boost(value),
                    lambda: write_cpu_boost(previous), {"cpu_boost": value})
                with _lock:
                    Plugin._state["cpu_boost"] = value
                    Plugin._state["cpu_boost_supported"] = True
        await self._offload(mutate)
        return await self.get_state()

    async def get_game_profile(self, app_id):
        app_id = self._app_id(app_id)
        with _lock:
            profiles = settings.getSetting("game_profiles", {})
        value = profiles.get(str(app_id)) if isinstance(profiles, dict) else None
        return {"exists": isinstance(value, dict),
                "profile": normalize_tdp(value) if isinstance(value, dict) else {},
                "preset": tdp_preset(value, value.get("preset"))
                if isinstance(value, dict) else ""}

    async def set_game_profile(self, app_id, raw, preset=None, expected_app_id=None):
        app_id = self._app_id(app_id)
        value = normalize_tdp(raw)
        profile_name = tdp_preset(value, preset)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                with _lock:
                    if not app_id or app_id != Plugin._active_app:
                        raise RuntimeError("game is no longer active")
                    previous = dict(Plugin._state["tdp"])
                    profiles = settings.getSetting("game_profiles", {})
                    profiles = dict(profiles) if isinstance(profiles, dict) else {}
                    if app_id not in profiles and len(profiles) >= 512:
                        raise RuntimeError("Maximum number of game profiles reached")
                    profiles[app_id] = {**value, "preset": profile_name}
                self._apply_and_save(lambda: self._apply_tdp_target(value),
                    self._tdp_rollback(previous), {"game_profiles": profiles})
                with _lock:
                    Plugin._state["tdp"] = value
                    Plugin._state["tdp_preset"] = profile_name

        await self._offload(mutate)
        return await self.get_state()

    async def delete_game_profile(self, app_id, expected_app_id=None):
        app_id = self._app_id(app_id)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                with _lock:
                    profiles = settings.getSetting("game_profiles", {})
                    profiles = dict(profiles) if isinstance(profiles, dict) else {}
                    profiles.pop(app_id, None)
                    target = normalize_tdp(settings.getSetting("tdp", DEFAULT_TDP))
                    profile_name = tdp_preset(
                        target, settings.getSetting("tdp_preset", None))
                    active = bool(app_id and app_id == Plugin._active_app)
                    previous = dict(Plugin._state["tdp"])
                if active:
                    self._apply_and_save(lambda: self._apply_tdp_target(target),
                        self._tdp_rollback(previous), {"game_profiles": profiles})
                else:
                    self._save("game_profiles", profiles)
                with _lock:
                    if active:
                        Plugin._state["tdp"] = target
                        Plugin._state["tdp_preset"] = profile_name

        await self._offload(mutate)
        return await self.get_state()

    async def set_active_app(self, app_id):
        app_id = self._app_id(app_id)
        if not supported_device():
            with _lock:
                Plugin._active_app = app_id
            return

        def mutate():
            with _tdp_mutation_lock:
                with _lock:
                    if app_id == Plugin._active_app:
                        return None
                    profiles = settings.getSetting("game_profiles", {})
                    profile = (profiles.get(app_id)
                               if app_id and isinstance(profiles, dict) else None)
                    target = normalize_tdp(
                        profile if isinstance(profile, dict)
                        else settings.getSetting("tdp", DEFAULT_TDP))
                    profile_name = tdp_preset(
                        target,
                        profile.get("preset") if isinstance(profile, dict)
                        else settings.getSetting("tdp_preset", None))
                previous = dict(Plugin._state["tdp"])
                restore = self._tdp_rollback(previous)
                try:
                    self._apply_tdp_target(target)
                except Exception as error:
                    try:
                        restore()
                    except Exception as rollback_error:
                        raise RuntimeError(f"{error}; TDP rollback failed: {rollback_error}") from error
                    raise
                with _lock:
                    Plugin._active_app = app_id
                    Plugin._state["tdp"] = target
                    Plugin._state["tdp_preset"] = profile_name
                return isinstance(profile, dict)

        used_profile = await self._offload(mutate)
        if used_profile is not None:
            decky.logger.info(
                f"{LOG} applied {'game ' + app_id if used_profile else 'global'} TDP")

    async def set_controller(self, raw):
        value = normalize_controller(raw)

        def mutate():
            with _controller_apply_lock:
                previous = dict(Plugin._state["controller"])
                self._apply_and_save(lambda: apply_controller(value),
                    lambda: apply_controller(previous), {"controller": value})
                with _lock:
                    Plugin._state["controller"] = value

        await self._offload(mutate)
        return await self.get_state()

    async def set_controller_with_vibration_feedback(self, raw):
        value = normalize_controller(raw)

        def mutate():
            with _controller_apply_lock:
                with _lock:
                    previous = dict(Plugin._state["controller"])
                self._apply_and_save(
                    lambda: apply_controller(value, True, previous["vibration"], False),
                    lambda: apply_controller(previous), {"controller": value})
                with _lock:
                    Plugin._state["controller"] = value

        await self._offload(mutate)
        return await self.get_state()

    async def set_vibration_gain(self, percent):
        value = _clamp(percent, 0, 100)

        def mutate():
            with _controller_apply_lock:
                with _lock:
                    controller = dict(Plugin._state["controller"])
                    previous_gain = controller["ff_gain"]
                    controller["ff_gain"] = value
                self._apply_and_save(lambda: set_vibration_gain(value),
                    lambda: set_vibration_gain(previous_gain), {"controller": controller})
                with _lock:
                    Plugin._state["controller"] = controller

        await self._offload(mutate)
        return await self.get_state()

    async def test_vibration(self, duration_ms=VIBRATION_TEST_MS):
        with _lock:
            level = Plugin._state["controller"]["vibration"]
        try:
            await self._offload(play_vibration_test, level, duration_ms)
            return {"success": True}
        except Exception as error:
            decky.logger.error(f"{LOG} vibration test failed: {error}")
            return {"success": False, "error": str(error)}

    async def set_charge_bypass(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with _tdp_mutation_lock:
                previous = read_charge_bypass()
                self._apply_and_save(lambda: write_charge_bypass(value),
                    lambda: write_charge_bypass(previous), {"charge_bypass": value})
                with _lock:
                    Plugin._state["charge_bypass"] = value
                    Plugin._state["charge_bypass_supported"] = True
        await self._offload(mutate)
        return await self.get_state()

    async def set_audio_fix(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with self._audio_mutation_lock:
                previous = bool(Plugin._state.get("audio_fix_enabled", False))
                def apply():
                    (apply_audio_fix if value else remove_audio_fix)()
                    if value and not audio_fix_ready():
                        raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
                try:
                    self._apply_and_save(apply,
                        apply_audio_fix if previous else remove_audio_fix,
                        {"audio_fix_enabled": value})
                except Exception as error:
                    installed = audio_fix_installed()
                    ready = audio_fix_ready() if installed else False
                    with _lock:
                        Plugin._state.update(audio_fix_enabled=previous,
                            audio_fix_error=str(error), audio_fix_installed=installed,
                            audio_calibration_available=previous and ready,
                            audio_profile="AYANEO v0.65" if ready else
                                "Installed, not active" if installed else "Generic fallback")
                    raise
                with _lock:
                    Plugin._state.update(audio_fix_enabled=value, audio_fix_error="",
                        audio_fix_installed=audio_fix_installed(),
                        audio_calibration_available=value,
                        audio_profile="AYANEO v0.65" if value else "Generic fallback")
        await self._offload(mutate)
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
        if not available or not await self._offload(audio_fix_ready):
            raise RuntimeError("apply the AYANEO audio fix successfully before recalibrating")
        try:
            result = await self._offload(perform_audio_recalibration)
            summary = (
                f"{result['timestamp']} · L {result['left']} · R {result['right']} · "
                f"{result['ambient']} °C · Restart required")
            decky.logger.info(
                f"{LOG} saved speaker calibration L={result['left']} R={result['right']} "
                f"backup={result['backup']}")
            try:
                await self._offload(self._save, "audio_calibration_last", summary)
            except Exception as error:
                raise RuntimeError(
                    f"Speaker calibration was applied, but its summary could not be saved: {error}. "
                    f"Previous calibration backup: {result['backup']}") from error
            with _lock:
                Plugin._state["audio_calibration_available"] = True
                Plugin._state["audio_calibration_last"] = summary
                Plugin._state["audio_fix_error"] = ""
        except Exception as error:
            ready = await self._offload(audio_fix_ready)
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
            Plugin._state["modules_reconnecting"] = True
            Plugin._state["modules_connected"] = False
            Plugin._state["modules_detached"] = False
            if side in ("left", "both"):
                Plugin._state["module_left"] = _module_info("left", status="ejecting")
            if side in ("right", "both"):
                Plugin._state["module_right"] = _module_info("right", status="ejecting")
        try:
            await self._offload(eject_controller_modules, side, controller)
        except Exception:
            try:
                presence = await self._offload(module_presence)
                left, right = module_states_from_presence(presence)
                connected = all(presence.values())
            except Exception:
                left = _module_info("left", status="unavailable")
                right = _module_info("right", status="unavailable")
                connected = False
            with _lock:
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_connected"] = connected
                Plugin._state["modules_detached"] = False
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
            raise
        decky.logger.info(f"{LOG} ejected {side} controller module(s)")
        return await self.get_state()

    async def reset_modules(self):
        with _lock:
            if Plugin._state.get("modules_reconnecting"):
                raise RuntimeError("insert both modules before resetting")
            controller = dict(Plugin._state["controller"])
            restore_buttons = Plugin._state.get("button_fix_installed", False)
            Plugin._state["modules_reconnecting"] = True
            Plugin._state["modules_detached"] = False
            Plugin._state["module_left"] = _module_info("left", status="activating")
            Plugin._state["module_right"] = _module_info("right", status="activating")
        try:
            await self._offload(reset_controller_modules, controller, restore_buttons)
            left, right = await self._offload(read_module_layout)
            with _lock:
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
                Plugin._state["modules_connected"] = True
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_detached"] = False
            decky.logger.info(f"{LOG} reset and re-detected both Magic Modules")
        except Exception:
            left = _module_info("left", status="unavailable")
            right = _module_info("right", status="unavailable")
            connected = False
            try:
                presence = await self._offload(module_presence)
                left, right = module_states_from_presence(presence)
                connected = all(presence.values())
            except Exception:
                pass
            with _lock:
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_connected"] = connected
                Plugin._state["modules_detached"] = False
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
            raise
        return await self.get_state()

    async def set_screen_fix(self, enabled, replace_existing=False):
        enabled = self._boolean(enabled)
        replace_existing = self._boolean(replace_existing)
        try:
            if enabled:
                backup = await self._offload(
                    install_display_script, bool(replace_existing))
                if backup is not None:
                    decky.logger.info(
                        f"{LOG} replaced existing display definition; backup={backup}")
                try:
                    await self._offload(patch_published_edid)
                except Exception as error:
                    # The Lua definition is already installed. The EDID file is
                    # recreated by gamescope and the monitor loop will retry it,
                    # so a transient race here must not roll back the UI switch.
                    decky.logger.warning(f"{LOG} initial EDID normalization deferred: {error}")
            else:
                await self._offload(remove_display_script)
        except Exception as error:
            decky.logger.error(f"{LOG} display definition change failed: {error}")
            raise
        return await self.get_state()

    async def _edid_loop(self):
        """Reapply after every gamescope session recreates its EDID copy."""
        while True:
            try:
                if _is_our_display_script(LUA_TARGET):
                    await self._offload(patch_published_edid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} EDID verification failed: {error}")
            await asyncio.sleep(1)

    async def set_button_fix(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with _controller_apply_lock:
                controller = dict(Plugin._state["controller"])
                paths = (INPUT_MAP_TARGET, LEGACY_INPUT_DEVICE_TARGET, *LEGACY_INPUT_MAP_TARGETS)
                before = {}
                for path in paths:
                    if path.is_symlink():
                        raise RuntimeError("refusing a symlinked InputPlumber override")
                    if path.exists():
                        if path == INPUT_MAP_TARGET and value and not button_map_owned(path):
                            raise RuntimeError("another aya7 InputPlumber override already exists")
                        if button_map_owned(path):
                            before[path] = path.read_bytes()
                previous_enabled = bool(before)
                try:
                    program_rear_buttons(value, controller if value else None)
                    (install_button_fix if value else remove_button_fix)()
                    _systemctl("restart", "inputplumber", check=True)
                except Exception as error:
                    failures = []
                    try:
                        program_rear_buttons(previous_enabled, controller if previous_enabled else None)
                    except Exception as recovery:
                        failures.append(f"controller: {recovery}")
                    for path in paths:
                        try:
                            if path in before:
                                if path.exists() and not button_map_owned(path):
                                    raise RuntimeError("the override changed outside Companion")
                                _atomic_write_bytes(path, before[path])
                            elif button_map_owned(path):
                                path.unlink()
                        except Exception as recovery:
                            failures.append(f"map: {recovery}")
                    try:
                        _systemctl("restart", "inputplumber", check=True)
                    except Exception as recovery:
                        failures.append(f"InputPlumber: {recovery}")
                    detail = "; rollback failed: " + "; ".join(failures) if failures else "; previous bindings restored"
                    raise RuntimeError(f"{error}{detail}") from error
                with _lock:
                    Plugin._state["button_fix_installed"] = value
        await self._offload(mutate)
        return await self.get_state()

    async def set_tm_guard(self, enabled):
        value = self._boolean(enabled)
        def commit():
            with _controller_apply_lock:
                self._save("tm_guard_enabled", value)
                with _lock:
                    Plugin._state["tm_guard_enabled"] = value
                    Plugin._state["tm_guard_status"] = "Monitoring" if value else "Disabled"
        await self._offload(commit)
        if value:
            with _lock:
                controller = dict(Plugin._state["controller"])
                restore_buttons = Plugin._state.get("button_fix_installed", False)
            try:
                changed = await self._offload(recover_tm_mode, controller, restore_buttons)
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
        """Retry pending boot work; each attempt reads the latest saved choices."""
        pending = {"tdp", "controller"}
        for key in ("charge_bypass", "cpu_boost"):
            if type(settings.getSetting(key, None)) is bool:
                pending.add(key)
        attempt = 0
        while pending:
            await asyncio.sleep((1, 2, 4, 8, 30, 60)[min(attempt, 5)])
            attempt += 1
            for key in tuple(sorted(pending)):
                try:
                    if key == "tdp":
                        await self._offload(Plugin._reapply_current_tdp)
                    elif key == "controller":
                        await self._offload(Plugin._restore_current_controller)
                    else:
                        await self._offload(self._restore_saved_control, key)
                    pending.remove(key)
                    decky.logger.info(f"{LOG} restored {key} after startup")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    decky.logger.warning(f"{LOG} {key} restore attempt failed: {error}")

    def _restore_saved_control(self, key, only_if_changed=False):
        with _tdp_mutation_lock:
            value = settings.getSetting(key, None)
            if type(value) is not bool:
                return
            read, write = ((read_cpu_boost, write_cpu_boost) if key == "cpu_boost"
                           else (read_charge_bypass, write_charge_bypass))
            if not only_if_changed or read() != value:
                write(value)
            with _lock:
                Plugin._state[key] = value
                Plugin._state[key + "_supported"] = True

    async def _restore_audio(self):
        """Retry late ALSA discovery without applying an obsolete enabled choice."""
        attempt = 0
        while True:
            await asyncio.sleep((1, 2, 4, 8, 30, 60)[min(attempt, 5)])
            attempt += 1
            try:
                if await self._offload(self._restore_audio_once):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with _lock:
                    Plugin._state["audio_calibration_available"] = False
                    Plugin._state["audio_fix_error"] = str(error)
                decky.logger.warning(f"{LOG} audio tuning attempt failed: {error}")

    def _restore_audio_once(self):
        with self._audio_mutation_lock:
            with _lock:
                if not Plugin._state.get("audio_fix_enabled", False):
                    return True
            if not audio_fix_ready():
                apply_audio_fix()
                if not audio_fix_ready():
                    raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
            with _lock:
                Plugin._state.update(audio_fix_supported=True, audio_fix_installed=True,
                    audio_calibration_available=True, audio_profile="AYANEO v0.65",
                    audio_fix_error="")
            return True

    async def _ac_loop(self):
        """Restore after charger/resume events and check managed CPU/charge drift."""
        previous = None
        suspend_offset = _suspend_clock_offset()
        next_control_check = 0.0
        pending_delays = []
        settle_at = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                current = await self._offload(ac_online)
                offset = _suspend_clock_offset()
                resumed = (offset is not None and suspend_offset is not None
                           and offset - suspend_offset > CONTROLLER_RESUME_THRESHOLD)
                if offset is not None:
                    suspend_offset = offset
                if (previous is not None and current != previous) or resumed:
                    pending_delays = [0.5, 1.0, 1.5, 3.0]
                    settle_at = time.monotonic() + pending_delays.pop(0)
                    next_control_check = 0.0
                previous = current
                now = time.monotonic()
                if pending_delays is not None and settle_at and now >= settle_at:
                    # Advance only after success; transient failures are retried.
                    try:
                        await self._offload(Plugin._reapply_current_tdp)
                    except Exception:
                        settle_at = now + 5.0
                        raise
                    settle_at = now + pending_delays.pop(0) if pending_delays else 0.0
                if now >= next_control_check:
                    next_control_check = now + 15.0
                    for key in ("cpu_boost", "charge_bypass"):
                        try:
                            await self._offload(self._restore_saved_control, key, True)
                        except Exception as error:
                            decky.logger.debug(f"{LOG} {key} verification deferred: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} power monitor retrying: {error}")

    async def _module_loop(self):
        """Track module identity and restore the controller after a module swap."""
        previous_connected = None
        restore_pending = False
        identify_pending = True
        retry_delay = 0.5
        identify_at = 0.0
        identify_delay = 0.5
        while True:
            await asyncio.sleep(retry_delay)
            try:
                powered = await self._offload(controller_powered)
                presence = await self._offload(module_presence)
                retry_delay = 0.5
                connected = all(presence.values())
                reconnected = previous_connected is False and connected
                previous_connected = connected
                with _lock:
                    Plugin._state["modules_connected"] = connected
                    reconnecting = Plugin._state.get("modules_reconnecting", False)
                    detached = Plugin._state.get("modules_detached", False)
                if not connected:
                    left, right = module_states_from_presence(presence)
                    with _lock:
                        if reconnecting:
                            Plugin._state["modules_detached"] = True
                        Plugin._state["module_left"] = left
                        Plugin._state["module_right"] = right
                    identify_pending = True
                    identify_at = 0.0
                    identify_delay = 0.5
                    continue
                # During eject, both EC presence bits can remain asserted until
                # the released module is physically lifted. Do not immediately
                # undo the deliberate controller power-off. Quick Reset also
                # owns the controller until its RPC clears this state.
                if reconnecting and not detached:
                    continue
                if not powered:
                    if not await self._offload(self._power_modules_if_allowed):
                        continue
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
                last_error = None
                if restore_pending:
                    for delay in (0.5, 1.0, 2.0, 3.0):
                        await asyncio.sleep(delay)
                        try:
                            if not await self._offload(both_modules_connected):
                                break
                            # Rear-button mappings are persisted with AYA_SAVE.
                            # Reprogramming them here would also issue the 0x88
                            # physical module reset on every reconnection.
                            await self._offload(Plugin._restore_current_controller)
                            with _lock:
                                if (Plugin._state.get("modules_reconnecting")
                                        and not Plugin._state.get("modules_detached")):
                                    break
                            restore_pending = False
                            identify_pending = True
                            with _lock:
                                Plugin._state["modules_reconnecting"] = False
                                Plugin._state["modules_detached"] = False
                            decky.logger.info(
                                f"{LOG} modules connected; restored RGB, vibration and FF_GAIN")
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            last_error = error
                if restore_pending and last_error:
                    decky.logger.warning(f"{LOG} controller setting restore failed: {last_error}")
                if identify_pending and not restore_pending and time.monotonic() >= identify_at:
                    try:
                        left, right = await self._offload(read_module_layout)
                        with _lock:
                            Plugin._state["module_left"] = left
                            Plugin._state["module_right"] = right
                        identify_pending = False
                        identify_delay = 0.5
                        decky.logger.info(
                            f"{LOG} modules detected: left {left['label']} (0x{left['code']:02X}), "
                            f"right {right['label']} (0x{right['code']:02X})")
                    except Exception as error:
                        identify_delay = min(30.0, identify_delay * 2)
                        identify_at = time.monotonic() + identify_delay
                        decky.logger.debug(f"{LOG} module identification pending: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retry_delay = min(30.0, retry_delay * 2)
                decky.logger.debug(f"{LOG} module monitor unavailable: {error}")

    @staticmethod
    def _power_modules_if_allowed():
        with _controller_apply_lock:
            with _lock:
                if (Plugin._state.get("modules_reconnecting")
                        and not Plugin._state.get("modules_detached")):
                    return False
            if not both_modules_connected():
                return False
            if not controller_powered():
                set_controller_power(True)
            return True

    async def _tm_guard_loop(self):
        """Repair controller resets and optionally undo accidental TM changes."""
        was_available = None
        last_device_token = None
        last_rumble_token = None
        next_gain_check = 0.0
        last_suspend_offset = _suspend_clock_offset()
        retry_delay = TM_GUARD_INTERVAL
        while True:
            await asyncio.sleep(retry_delay)
            suspend_offset = _suspend_clock_offset()
            resumed = bool(
                suspend_offset is not None
                and last_suspend_offset is not None
                and suspend_offset - last_suspend_offset > CONTROLLER_RESUME_THRESHOLD
            )
            if suspend_offset is not None:
                last_suspend_offset = suspend_offset
            with _lock:
                enabled = Plugin._state.get("tm_guard_enabled", False)
                reconnecting = Plugin._state.get("modules_reconnecting", False)
                connected = Plugin._state.get("modules_connected", False)
            if reconnecting or not connected:
                was_available = None
                last_device_token = None
                last_rumble_token = None
                next_gain_check = 0.0
                with _lock:
                    Plugin._state["tm_guard_status"] = (
                        "Waiting for modules" if enabled else "Disabled")
                continue
            try:
                status, path = await self._offload(
                    Plugin._reconcile_current_controller,
                    enabled, was_available is False or resumed)
                device_token = await self._offload(_device_node_token, path)
                if (last_device_token is not None
                        and device_token != last_device_token
                        and status == "healthy"):
                    status, path = await self._offload(
                        Plugin._reconcile_current_controller, enabled, True)
                    device_token = await self._offload(_device_node_token, path)
                was_available = True
                retry_delay = TM_GUARD_INTERVAL
                last_device_token = device_token
                if resumed and status in ("mode_restored", "configuration_restored"):
                    decky.logger.info(
                        f"{LOG} restored controller configuration after system resume")
                now = time.monotonic()
                if now >= next_gain_check:
                    rumble_token = await self._offload(_rumble_event_token)
                    if rumble_token != last_rumble_token:
                        if rumble_token is not None and status in ("healthy", "tm_mode"):
                            await self._offload(
                                Plugin._restore_current_vibration_gain)
                            decky.logger.info(
                                f"{LOG} restored FF_GAIN after input-device recreation")
                        last_rumble_token = rumble_token
                    next_gain_check = now + CONTROLLER_GAIN_CHECK_INTERVAL
                with _lock:
                    if status == "mode_restored":
                        Plugin._state["tm_guard_recoveries"] += 1
                    Plugin._state["tm_guard_status"] = (
                        "Custom mode restored"
                        if enabled and status == "mode_restored"
                        else "Monitoring" if enabled else "Disabled")
                if status == "mode_restored":
                    decky.logger.info(f"{LOG} TM Guard restored custom controller mode")
                elif status == "configuration_restored":
                    decky.logger.info(
                        f"{LOG} restored drifted RGB, vibration and FF_GAIN")
                elif status == "tm_mode":
                    # TM Guard is intentionally disabled, so leave the user's
                    # selected hardware mode untouched.
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                was_available = False
                retry_delay = min(30.0, retry_delay * 2)
                with _lock:
                    Plugin._state["tm_guard_status"] = (
                        "Waiting for controller" if enabled else "Disabled")
                decky.logger.debug(f"{LOG} controller monitor waiting: {error}")

    async def _main(self):
        if self._cleanup_task is not None:
            await complete(self._cleanup_task)
            self._cleanup_task = None
        if self._startup_task is not None and not self._startup_task.done():
            return await complete(self._startup_task)
        self._closing = False
        self._initializing = True
        updater.reset()
        with _lock:
            Plugin._state = {"initializing": True}
        try:
            self._startup_task = asyncio.create_task(self._start())
            await complete(self._startup_task)
        except asyncio.CancelledError:
            await self._unload()
            raise
        except Exception as error:
            with _lock:
                Plugin._state["startup_error"] = str(error)
            decky.logger.error(f"{LOG} startup failed: {error}")
        finally:
            self._initializing = False
            with _lock:
                Plugin._state["initializing"] = False

    async def _start(self):
        await self._offload(settings.read)
        is_supported = await self._offload(supported_device)
        storage_error = getattr(settings, "recovery_error", "")
        hardware_allowed = is_supported and not storage_error
        try:
            await self._offload(updater.ssl_context)
        except Exception as error:
            decky.logger.warning(f"{LOG} updater TLS initialization failed: {error}")
        saved_controller = normalize_controller(settings.getSetting("controller", DEFAULT_CONTROLLER))
        # Treat an existing map as an enabled toggle and migrate it in place
        # when a newer package extends the aya7 mapping.
        key_binding_installed = any(button_map_owned(path) for path in (
            INPUT_MAP_TARGET, *LEGACY_INPUT_MAP_TARGETS, LEGACY_INPUT_DEVICE_TARGET))
        if hardware_allowed and key_binding_installed:
            try:
                await self._offload(install_button_fix)
            except Exception as error:
                decky.logger.warning(f"{LOG} key-binding migration failed: {error}")
            key_binding_installed = await self._offload(button_fix_installed)
        screen_installed = _display_script_owned(LUA_TARGET)
        if hardware_allowed and screen_installed and not _is_our_display_script(LUA_TARGET):
            try:
                await self._offload(install_display_script)
            except Exception as error:
                decky.logger.warning(f"{LOG} display-definition migration failed: {error}")
            screen_installed = _is_our_display_script(LUA_TARGET)
        audio_enabled = settings.getSetting("audio_fix_enabled", True) is not False
        tm_guard_enabled = settings.getSetting("tm_guard_enabled", True) is not False
        audio_installed = await self._offload(audio_fix_installed) if hardware_allowed else False
        audio_ready = await self._offload(audio_fix_ready) if audio_installed else False
        ec_control = await self._offload(ensure_charge_control) if hardware_allowed else None
        charge_control = await self._offload(ensure_charge_bypass_control) if hardware_allowed else None
        try:
            charge_bypass = await self._offload(read_charge_bypass) if charge_control else False
        except (OSError, RuntimeError):
            charge_control = None
            charge_bypass = False
        saved_tdp = normalize_tdp(settings.getSetting("tdp", DEFAULT_TDP))
        try:
            cpu_boost = await self._offload(read_cpu_boost) if hardware_allowed else False
            cpu_boost_supported = is_supported
        except RuntimeError:
            cpu_boost = False
            cpu_boost_supported = False
        Plugin._state = {
            "supported": is_supported,
            "settings_error": storage_error,
            "initializing": False,
            "startup_error": "",
            "device": _dmi("product_name") or "unknown",
            "version": updater.plugin_version(),
            "tdp_backend": await self._offload(tdp_backend) if hardware_allowed else "Unavailable",
            "tdp": saved_tdp,
            "tdp_preset": tdp_preset(saved_tdp, settings.getSetting("tdp_preset", None)),
            "presets": PRESETS,
            "cpu_boost_supported": cpu_boost_supported,
            "cpu_boost": cpu_boost,
            "controller": saved_controller,
            "screen_installed": screen_installed,
            "screen_conflict": _display_script_conflict(LUA_TARGET),
            "edid_patched": False,
            "edid_game_nits": 0,
            "button_fix_installed": key_binding_installed,
            "charge_bypass_supported": charge_control is not None,
            "charge_bypass": charge_bypass,
            "module_eject_supported": ec_control is not None,
            "module_reset_supported": ec_control is not None,
            "modules_reconnecting": False,
            "modules_connected": True,
            "modules_detached": False,
            "module_left": _module_info("left", status="detecting"),
            "module_right": _module_info("right", status="detecting"),
            "tm_guard_enabled": tm_guard_enabled,
            "tm_guard_status": "Monitoring" if tm_guard_enabled else "Disabled",
            "tm_guard_recoveries": 0,
            "gpu_power_w": None,
            "audio_fix_supported": (
                await self._offload(audio_fix_supported) if hardware_allowed else False),
            "audio_fix_enabled": audio_enabled,
            "audio_fix_installed": audio_installed,
            "audio_calibration_available": audio_enabled and audio_ready,
            "audio_calibration_last": settings.getSetting("audio_calibration_last", ""),
            "audio_profile": "AYANEO v0.65" if audio_ready else
                             ("Pending" if audio_enabled else "Generic fallback"),
            "audio_fix_error": "",
        }
        Plugin._active_app = ""
        if hardware_allowed and not self._closing:
            Plugin._restore_task = asyncio.create_task(self._restore_hardware())
            Plugin._edid_task = asyncio.create_task(self._edid_loop())
            Plugin._ac_task = asyncio.create_task(self._ac_loop())
            Plugin._module_task = asyncio.create_task(self._module_loop())
            Plugin._tm_guard_task = asyncio.create_task(self._tm_guard_loop())
            if audio_enabled:
                Plugin._audio_task = asyncio.create_task(self._restore_audio())
        decky.logger.info(f"{LOG} started on {Plugin._state['device']}")

    async def _unload(self):
        if self._cleanup_task is not None:
            return await complete(self._cleanup_task)
        self._closing = True
        updater.close()
        async def cleanup():
            if self._startup_task is not None and not self._startup_task.done():
                self._startup_task.cancel()
                await asyncio.gather(self._startup_task, return_exceptions=True)
            tasks = [task for task in (
                Plugin._audio_task, Plugin._restore_task, Plugin._edid_task,
                Plugin._ac_task, Plugin._module_task, Plugin._tm_guard_task,
            ) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            while self._rpc_jobs or self._workers:
                await asyncio.gather(*tuple(self._rpc_jobs | self._workers), return_exceptions=True)
            for name in ("_audio_task", "_restore_task", "_edid_task", "_ac_task",
                         "_module_task", "_tm_guard_task"):
                setattr(Plugin, name, None)
            with _lock:
                was_supported = bool(Plugin._state.get("supported", False))
                was_reconnecting = bool(Plugin._state.get("modules_reconnecting", False))
            if (was_supported and not was_reconnecting
                    and not getattr(settings, "recovery_error", "")
                    and await self._offload(supported_device)):
                try:
                    connected = await self._offload(both_modules_connected)
                    if connected and not await self._offload(controller_powered):
                        await self._offload(set_controller_power, True)
                except Exception as error:
                    decky.logger.warning(f"{LOG} could not restore controller power on unload: {error}")
            decky.logger.info(f"{LOG} unloaded")
        self._cleanup_task = asyncio.create_task(cleanup())
        await complete(self._cleanup_task)

    async def _uninstall(self):
        await complete(self._remove_owned())

    async def _remove_owned(self):
        await self._unload()
        is_supported = await self._offload(supported_device)
        if is_supported:
            try:
                await self._offload(remove_audio_fix, False)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not remove audio firmware path: {error}")
        if is_supported and settings.getSetting("charge_bypass", False):
            try:
                await self._offload(write_charge_bypass, False)
                decky.logger.info(f"{LOG} restored automatic charging before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore charging before uninstall: {error}")
        if is_supported and settings.getSetting("cpu_boost", True) is False:
            try:
                await self._offload(write_cpu_boost, True)
                decky.logger.info(f"{LOG} restored CPU Boost before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore CPU Boost before uninstall: {error}")
        if is_supported and _display_script_owned(LUA_TARGET):
            try:
                await self._offload(remove_display_script)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not remove display definition: {error}")
        if is_supported:
            try:
                await self._offload(program_rear_buttons, False)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not clear LC1/RC1 bindings: {error}")
        if is_supported:
            await self._offload(remove_button_fix)
            await self._offload(_systemctl, "restart", "inputplumber")
