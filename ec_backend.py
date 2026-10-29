# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 embedded controller: charging, battery status, controller power and module presence."""

import glob
import subprocess
import threading
from pathlib import Path

import decky

import companion_common
import dk_process
from companion_common import LOG


POWER_SUPPLY_ROOT = Path("/sys/class/power_supply")
AYANEO_EC_ROOT = Path("/sys/devices/platform/ayaneo-ec")
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
_ec_lock = threading.Lock()
_charge_apply_lock = threading.Lock()


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
    result = dk_process.run([
        "busctl", "--system", "get-property", "org.freedesktop.UPower",
        f"/org/freedesktop/UPower/devices/battery_{battery_name}",
        "org.freedesktop.UPower.Device", "TimeToFull",
    ], 3, capture_output=True, text=True)
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
        result = dk_process.run(["modprobe", "ec_sys", "write_support=1"], 10,
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
    if not companion_common.supported_device():
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
    companion_common.require_supported_device("embedded-controller writes")
    path = ensure_charge_control()
    if path is None:
        raise RuntimeError("AYANEO EC write access is unavailable")
    with _ec_lock, path.open("r+b", buffering=0) as ec:
        ec.seek(register)
        previous = ec.read(1)
        if len(previous) != 1:
            raise RuntimeError(f"could not read AYANEO EC register 0x{register:02x}")
        expected = bytes([value])
        try:
            ec.seek(register)
            if ec.write(expected) != 1:
                raise RuntimeError(f"could not write AYANEO EC register 0x{register:02x}")
            ec.seek(register)
            if ec.read(1) != expected:
                raise RuntimeError(f"AYANEO EC register 0x{register:02x} did not retain the setting")
        except Exception as error:
            try:
                ec.seek(register)
                if ec.write(previous) != 1:
                    raise RuntimeError("short EC rollback write")
                ec.seek(register)
                if ec.read(1) != previous:
                    raise RuntimeError("EC rollback readback mismatch")
            except Exception as recovery:
                raise RuntimeError(f"{error}; EC recovery failed: {recovery}") from error
            raise


def _native_controller_power() -> bool:
    value = (AYANEO_EC_ROOT / "controller_power").read_text().strip()
    if value not in ("0", "1"):
        raise RuntimeError(f"unexpected AYANEO controller power state: {value!r}")
    return value == "1"


def _native_module_presence() -> dict:
    value = (AYANEO_EC_ROOT / "controller_modules").read_text().strip()
    if value not in ("both", "left", "right", "none"):
        raise RuntimeError(f"unexpected AYANEO controller module state: {value!r}")
    return {"left": value in ("both", "left"),
            "right": value in ("both", "right")}


def ensure_controller_control() -> Path | None:
    """Prefer the kernel ABI; use raw EC only when ayaneo-ec is absent."""
    if AYANEO_EC_ROOT.exists():
        try:
            _native_controller_power()
            _native_module_presence()
            return AYANEO_EC_ROOT
        except (OSError, RuntimeError) as error:
            decky.logger.warning(f"{LOG} native controller ABI unavailable: {error}")
            return None
    return ensure_charge_control()


def controller_powered() -> bool:
    if AYANEO_EC_ROOT.exists():
        return _native_controller_power()
    return _read_ec_register(EC_CONTROLLER_POWER_REGISTER) == EC_CONTROLLER_POWER_ON


def both_modules_connected() -> bool:
    if AYANEO_EC_ROOT.exists():
        return all(_native_module_presence().values())
    # Bits set in this register mean that the corresponding module is absent.
    return (_read_ec_register(EC_MODULE_REGISTER) & EC_MODULE_MASK) == 0


def module_presence() -> dict:
    """Return each physical slot state from the AYANEO EC."""
    if AYANEO_EC_ROOT.exists():
        return _native_module_presence()
    value = _read_ec_register(EC_MODULE_REGISTER)
    return {
        "left": not bool(value & EC_MODULE_LEFT),
        "right": not bool(value & EC_MODULE_RIGHT),
    }


def set_controller_power(enabled: bool) -> None:
    companion_common.require_supported_device("controller power")
    if type(enabled) is not bool:
        raise ValueError("controller power must be a boolean")
    if AYANEO_EC_ROOT.exists():
        path = AYANEO_EC_ROOT / "controller_power"
        with _ec_lock:
            previous = _native_controller_power()
            try:
                path.write_text("1\n" if enabled else "0\n")
                if _native_controller_power() != enabled:
                    raise RuntimeError("kernel controller power did not retain the setting")
            except Exception as error:
                try:
                    path.write_text("1\n" if previous else "0\n")
                    if _native_controller_power() != previous:
                        raise RuntimeError("controller power rollback readback mismatch")
                except Exception as recovery:
                    raise RuntimeError(f"{error}; controller recovery failed: {recovery}") from error
                raise
        return
    _write_ec_register(EC_CONTROLLER_POWER_REGISTER,
                       EC_CONTROLLER_POWER_ON if enabled else EC_CONTROLLER_POWER_OFF)
