# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/Ayaneo3Companion

"""AYANEO 3 hardware controls for Decky Loader."""

import asyncio
import colorsys
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
ALLOWED_DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})
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
INPUT_DEVICE_SOURCE = PLUGIN_DIR / "assets" / "01-ayaneo3-companion.yaml"
INPUT_MAP_SOURCE = PLUGIN_DIR / "assets" / "ayaneo3-companion.yaml"
INPUT_DEVICE_TARGET = Path("/etc/inputplumber/devices.d/01-ayaneo3-companion.yaml")
INPUT_MAP_TARGET = Path("/etc/inputplumber/capability_maps.d/ayaneo3-companion.yaml")
EC_CHARGE_REGISTER = 0x1E
EC_CHARGE_AUTO = 0xAA
EC_CHARGE_INHIBIT = 0x55
EC_CONTROLLER_POWER_REGISTER = 0x2D
EC_CONTROLLER_POWER_OFF = 0xFE
EC_CONTROLLER_POWER_ON = 0xFF
EC_MODULE_REGISTER = 0x2F
EC_MODULE_MASK = 0x03

VIBRATION_VALUES = {"off": 0x04, "low": 0x01, "medium": 0x02, "high": 0x03}
RGB_MODES = {"off": 0xFF, "solid": 0x01, "pulse": 0x02, "rainbow": 0x03}
EVIOCGBIT_FF = 0x80204535
EVIOCSFF = 0x40304580
EVIOCRMFF = 0x40044581
EV_FF = 0x15
FF_RUMBLE = 0x50
DEFAULT_CONTROLLER = {
    "vibration": "high", "rgb_mode": "solid", "color": "6600ff", "brightness": 100,
}
DEFAULT_TDP = {"spl": 15, "sppt": 18, "fppt": 25}
PRESETS = {
    "Low power": {"spl": 8, "sppt": 10, "fppt": 12},
    "Balanced": {"spl": 15, "sppt": 18, "fppt": 25},
    "Performance": {"spl": 30, "sppt": 32, "fppt": 35},
    "Max": {"spl": 35, "sppt": 38, "fppt": 40},
}

settings = SettingsManager(name="settings", settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR)
_lock = threading.RLock()
_tdp_apply_lock = threading.Lock()
_ec_lock = threading.Lock()
_controller_apply_lock = threading.Lock()

def _dmi(name: str) -> str:
    try:
        return Path(f"/sys/class/dmi/id/{name}").read_text().strip()
    except OSError:
        return ""


def supported_device() -> bool:
    return _dmi("sys_vendor").upper() == "AYANEO" and _dmi("product_name").upper() == "AYANEO 3"


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
    with path.open("rb", buffering=0) as ec:
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
    with path.open("r+b", buffering=0) as ec:
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


def set_controller_power(enabled: bool) -> None:
    _write_ec_register(EC_CONTROLLER_POWER_REGISTER,
                       EC_CONTROLLER_POWER_ON if enabled else EC_CONTROLLER_POWER_OFF)


def _clamp(value, low, high):
    return max(low, min(high, int(value)))


def normalize_tdp(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    spl = _clamp(source.get("spl", DEFAULT_TDP["spl"]), 5, 35)
    sppt = _clamp(source.get("sppt", DEFAULT_TDP["sppt"]), spl, 40)
    fppt = _clamp(source.get("fppt", DEFAULT_TDP["fppt"]), sppt, 45)
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
        "rgb_mode": mode if mode in RGB_MODES else "solid",
        "color": _hex_color(source.get("color")),
        "brightness": _clamp(source.get("brightness", 100), 0, 100),
    }


def _pad(data, length=65) -> bytes:
    return bytes(data).ljust(length, b"\0")


AYA_CHECK = _pad([0, 0, 0, 0, 0x08])
AYA_CUSTOM = _pad([0, 0, 0, 0, 0x0A, 0x02])
AYA_SAVE = _pad([0, 0, 0, 0, 0x05])


def _checksum(command) -> bytes:
    data = bytearray(_pad(command))
    data[1:3] = sum(data[7:]).to_bytes(2, "little")
    return bytes(data)


def _rgb_bytes(config: dict):
    if config["rgb_mode"] == "off":
        return 0, 0, 0
    text = config["color"]
    r, g, b = (int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    h = (h + 10 / 360) % 1.0
    v = min(v, config["brightness"] / 100)
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))


def controller_command(config: dict, eject: str | None = None) -> bytes:
    config = normalize_controller(config)
    mode = RGB_MODES[config["rgb_mode"]]
    r, g, b = _rgb_bytes(config)
    vibration = VIBRATION_VALUES[config["vibration"]] << 4
    command = bytearray(65)
    command[3:5] = bytes((0x21, 0x09))
    command[8:12] = bytes((mode, r, g, b))
    command[12:16] = bytes((mode, r, g, b))
    command[20] = {None: 0x00, "left": 0x07, "right": 0x70, "both": 0x77}.get(eject, 0x00)
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
        "rgb_mode": reverse_modes.get(response[7], "solid"),
        "color": bytes((r, g, b)).hex(),
        "brightness": 100,
    }


def apply_controller(config: dict) -> None:
    with _controller_apply_lock:
        path = _vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            if len(check) > 17 and check[17] == 1:
                _hid_exchange(fd, AYA_CUSTOM)
            if not _hid_exchange(fd, controller_command(config)):
                raise RuntimeError("controller rejected configuration")
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
            if len(check) > 17 and check[17] == 1:
                _hid_exchange(fd, AYA_CUSTOM)
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
        if vendor == "1c4f":
            return node
        fallback = fallback or node
    return fallback


def play_vibration_test(level: str, duration_ms: int = 500) -> None:
    """Play one FF_RUMBLE effect without changing the saved firmware level."""
    import fcntl
    import time

    strength = {"low": 0.33, "medium": 0.66, "high": 1.0}.get(level, 0.0)
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
        headers={"User-Agent": "Ayaneo3Companion/0.4.1"},
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


class Plugin:
    _state = {}
    _restore_task = None
    _edid_task = None
    _ac_task = None
    _module_task = None
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
            state["gpu_power_w"] = gpu_power_watts()
            state["screen_installed"] = _is_our_display_script(LUA_TARGET)
            try:
                edid = PUBLISHED_EDID.read_bytes()
                state["edid_game_nits"] = round(_published_edid_nits(edid) or 0)
            except OSError:
                state["edid_game_nits"] = 0
            state["edid_patched"] = state["edid_game_nits"] == EDID_TARGET_NITS
            state["button_fix_installed"] = (
                INPUT_DEVICE_TARGET.exists() and INPUT_MAP_TARGET.exists()
                and INPUT_DEVICE_TARGET.read_bytes() == INPUT_DEVICE_SOURCE.read_bytes()
                and INPUT_MAP_TARGET.read_bytes() == INPUT_MAP_SOURCE.read_bytes())
            try:
                state["charge_bypass"] = read_charge_bypass()
                state["charge_bypass_supported"] = True
            except (OSError, RuntimeError):
                state["charge_bypass_supported"] = False
            try:
                state["modules_connected"] = both_modules_connected()
                state["module_eject_supported"] = True
            except (OSError, RuntimeError):
                state["module_eject_supported"] = False
            return state

    async def get_state(self):
        return await asyncio.to_thread(self._snapshot)

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

    async def test_vibration(self, duration_ms=500):
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
        decky.logger.info(f"{LOG} ejected {side} controller module(s)")
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
            INPUT_DEVICE_TARGET.parent.mkdir(parents=True, exist_ok=True)
            INPUT_MAP_TARGET.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(INPUT_DEVICE_SOURCE, INPUT_DEVICE_TARGET)
            shutil.copyfile(INPUT_MAP_SOURCE, INPUT_MAP_TARGET)
        else:
            for source, target in ((INPUT_DEVICE_SOURCE, INPUT_DEVICE_TARGET),
                                   (INPUT_MAP_SOURCE, INPUT_MAP_TARGET)):
                if target.exists() and target.read_bytes() == source.read_bytes():
                    target.unlink()
        subprocess.run(["systemctl", "restart", "inputplumber"], check=True, timeout=15)
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
                    pending.remove("controller")
                    decky.logger.info(f"{LOG} restored RGB and vibration after startup")
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
        """Power the controller back on after both replacement modules are seated."""
        while True:
            await asyncio.sleep(0.5)
            try:
                powered = await asyncio.to_thread(controller_powered)
                connected = await asyncio.to_thread(both_modules_connected)
                with _lock:
                    Plugin._state["modules_connected"] = connected
                    reconnecting = Plugin._state.get("modules_reconnecting", False)
                if not powered and connected:
                    await asyncio.to_thread(set_controller_power, True)
                    await asyncio.sleep(1)
                    with _lock:
                        Plugin._state["modules_reconnecting"] = False
                        controller = dict(Plugin._state["controller"])
                    try:
                        await asyncio.to_thread(apply_controller, controller)
                    except Exception as error:
                        decky.logger.warning(f"{LOG} controller setting restore failed: {error}")
                    decky.logger.info(f"{LOG} replacement modules connected; controller powered on")
                elif reconnecting and powered:
                    # Recover if firmware powered the controller itself.
                    with _lock:
                        Plugin._state["modules_reconnecting"] = False
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.debug(f"{LOG} module monitor unavailable: {error}")

    async def _main(self):
        await asyncio.to_thread(settings.read)
        is_supported = await asyncio.to_thread(supported_device)
        saved_controller = normalize_controller(settings.getSetting("controller", DEFAULT_CONTROLLER))
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
            "button_fix_installed": False,
            "charge_bypass_supported": charge_control is not None,
            "charge_bypass": charge_bypass,
            "module_eject_supported": charge_control is not None,
            "modules_reconnecting": False,
            "modules_connected": True,
            "gpu_power_w": None,
        }
        Plugin._active_app = ""
        if is_supported:
            Plugin._restore_task = asyncio.create_task(self._restore_hardware())
            Plugin._edid_task = asyncio.create_task(self._edid_loop())
            Plugin._ac_task = asyncio.create_task(self._ac_loop())
            Plugin._module_task = asyncio.create_task(self._module_loop())
        decky.logger.info(f"{LOG} started on {Plugin._state['device']}")

    async def _unload(self):
        if Plugin._restore_task:
            Plugin._restore_task.cancel()
            await asyncio.wait([Plugin._restore_task], timeout=1.0)
            Plugin._restore_task = None
        if Plugin._edid_task:
            Plugin._edid_task.cancel()
            await asyncio.wait([Plugin._edid_task], timeout=1.0)
            Plugin._edid_task = None
        if Plugin._ac_task:
            Plugin._ac_task.cancel()
            await asyncio.wait([Plugin._ac_task], timeout=1.0)
            Plugin._ac_task = None
        if Plugin._module_task:
            Plugin._module_task.cancel()
            await asyncio.wait([Plugin._module_task], timeout=1.0)
            Plugin._module_task = None
        try:
            if not await asyncio.to_thread(controller_powered):
                await asyncio.to_thread(set_controller_power, True)
        except Exception as error:
            decky.logger.warning(f"{LOG} could not restore controller power on unload: {error}")
        decky.logger.info(f"{LOG} unloaded")

    async def _uninstall(self):
        if settings.getSetting("charge_bypass", False):
            try:
                await asyncio.to_thread(write_charge_bypass, False)
                decky.logger.info(f"{LOG} restored automatic charging before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore charging before uninstall: {error}")
        if _is_our_display_script(LUA_TARGET):
            LUA_TARGET.unlink()
        for source, target in ((INPUT_DEVICE_SOURCE, INPUT_DEVICE_TARGET),
                               (INPUT_MAP_SOURCE, INPUT_MAP_TARGET)):
            if target.exists() and source.exists() and target.read_bytes() == source.read_bytes():
                target.unlink()
        subprocess.run(["systemctl", "restart", "inputplumber"], check=False)
