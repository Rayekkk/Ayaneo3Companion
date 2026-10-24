# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 controller: vendor HID protocol, Magic Modules, rear buttons, RGB and force feedback."""

import colorsys
import glob
import os
import select
import struct
import threading
import time
from pathlib import Path

import decky

import companion_common
import ec_backend
from companion_common import LOG


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
HID_COMMAND_ATTEMPTS = 3
HID_AYANEO_DRIVER_NAMES = frozenset({"hid-ayaneo", "hid_ayaneo"})
CONTROLLER_COLOR_TOLERANCE = 1
# The internal controller's own USB port (vendor HID and its keyboard). 1c4f:0002 alone
# is SiGma Micro's generic keyboard chip, which external USB keyboards use too.
AYA3_CONTROLLER_PHYS_PREFIXES = ("usb-0000:c4:00.3-1/", "usb-0000:c7:00.0-1/")
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
DEFAULT_CONTROLLER = {
    "vibration": "high", "ff_gain": 100,
    "rgb_mode": "solid", "color": "6600ff", "brightness": 100,
}
controller_apply_lock = threading.RLock()
_controller_event_lock = threading.Lock()


def module_info(side: str, code: int | None = None, status: str = "connected") -> dict:
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
        return module_info("left", status="detecting"), module_info("right", status="detecting")
    if left:
        return module_info("left", status="unpowered"), module_info("right", status="disconnected")
    if right:
        return module_info("left", status="disconnected"), module_info("right", status="unpowered")
    return module_info("left", status="disconnected"), module_info("right", status="disconnected")


def _hex_color(value) -> str:
    text = str(value or "").strip().lstrip("#").lower()
    return text if len(text) == 6 and all(c in "0123456789abcdef" for c in text) else "6600ff"


def normalize_controller(raw) -> dict:
    source = raw if isinstance(raw, dict) else {}
    vibration = str(source.get("vibration", "high")).lower()
    mode = str(source.get("rgb_mode", "solid")).lower()
    return {
        "vibration": vibration if vibration in VIBRATION_VALUES else "high",
        "ff_gain": companion_common.clamp(source.get("ff_gain", 100), 0, 100),
        "rgb_mode": mode if mode in RGB_MODES else "solid",
        "color": _hex_color(source.get("color")),
        "brightness": companion_common.clamp(source.get("brightness", 100), 0, 100),
    }


def _pad(data, length=65) -> bytes:
    return bytes(data).ljust(length, b"\0")


AYA_CHECK = _pad([0, 0, 0, 0, 0x08])
AYA_CUSTOM = _pad([0, 0, 0, 0, 0x0A, 0x02])
AYA_SAVE = _pad([0, 0, 0, 0, 0x05])
AYA_CUSTOM_REQUIRED_INDEX = 18
# Response byte 18 is the controller mode: 2 is the custom mode Companion
# programs. Measured on the console: the TM button switches to 3; HHD and
# retail HX 370 hardware report 1 for the other firmware mode.
AYA_NON_CUSTOM_MODES = frozenset({1, 3})


def controller_requires_custom(response: bytes) -> bool:
    # This status flag is not part of the one-byte-shifted RGB payload.
    return len(response) > AYA_CUSTOM_REQUIRED_INDEX and \
        response[AYA_CUSTOM_REQUIRED_INDEX] in AYA_NON_CUSTOM_MODES


def decode_module_layout(response: bytes) -> tuple[dict, dict]:
    """Decode the module identifiers returned by AYANEO's AYA_CHECK command."""
    if len(response) <= MODULE_INFO_RIGHT_INDEX:
        raise RuntimeError("controller response does not contain Magic Module information")
    left = response[MODULE_INFO_LEFT_INDEX]
    right = response[MODULE_INFO_RIGHT_INDEX]
    if not left or not right:
        raise RuntimeError("controller has not identified both Magic Modules yet")
    return module_info("left", left), module_info("right", right)


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
    # QAM: the firmware's own F24. HHD programs Left Meta + D here and reads the
    # D itself; InputPlumber's Meta+D chord never opened QAM (checked on the console).
    0x17: (0x00, 0x00, 0x73),  # F24 / QAM
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


_vendor_hidraw_cached: tuple[str, tuple[int, int]] | None = None


def vendor_hidraw() -> str:
    # Asked every half second: reuse the node while the same device node exists.
    # A driver change or re-enumeration recreates it, which a stat notices.
    global _vendor_hidraw_cached
    if _vendor_hidraw_cached is not None:
        path, key = _vendor_hidraw_cached
        try:
            info = os.stat(path)
            if (info.st_ino, getattr(info, "st_rdev", 0)) == key:
                return path
        except OSError:
            pass
        _vendor_hidraw_cached = None
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        device_path = Path(sys_path).resolve() / "device"
        try:
            descriptor = (device_path / "report_descriptor").read_bytes()
            uevent = (device_path / "uevent").read_text()
        except OSError:
            continue
        if (descriptor.startswith(b"\x06\x00\xff\x09\x01") and "HID_ID=0003:00001C4F:00000002" in uevent
                and any(f"HID_PHYS={prefix}" in uevent for prefix in AYA3_CONTROLLER_PHYS_PREFIXES)):
            if _kernel_hid_ayaneo_active(device_path):
                raise RuntimeError(
                    "kernel hid-ayaneo is active; direct controller commands are disabled "
                    "to prevent conflicting HID transactions"
                )
            path = "/dev/" + Path(sys_path).name
            try:
                info = os.stat(path)
                _vendor_hidraw_cached = (path, (info.st_ino, getattr(info, "st_rdev", 0)))
            except OSError:
                pass
            return path
    raise RuntimeError("AYANEO vendor HID interface not found")


def _hid_exchange(fd, command: bytes, timeout=0.4, attempts=HID_COMMAND_ATTEMPTS) -> bytes:
    """Send a command and retry the complete transaction if it is unanswered.

    attempts=1 for commands that must not run twice when only the answer is late:
    an NVRAM save, an eject (latch motors) and the physical module reset.
    """
    for _ in range(attempts):
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
            _reset_stale_tm_gamepad()
            _consume_power_cycle()  # the next TM press gets its own power cycle
            return True
    raise RuntimeError("controller did not enter custom mode")


USB_DEVICES = Path("/sys/bus/usb/devices")
TM_GAMEPAD_ID = ("4001", "0428")
USBDEVFS_RESET = 0x5514  # _IO('U', 20)
TM_GAMEPAD_RETURN_S = 5.0
TM_POWER_OFF_S = 2.0
TM_SWITCH_WINDOW_S = 30.0
# Consecutive power cycles that did not bring back a clean custom mode before TM Guard gives up.
TM_POWER_CYCLE_LIMIT = 3
_tm_power_cycled_at = None
_tm_power_cycles = 0


def power_cycle_controller() -> None:
    """Cut the controller's power through the EC, then restore it.

    Measured on the console: leaving TM mode with AYA_CUSTOM alone can leave a
    key held in the firmware's keyboard report (an arrow that is never
    released, also merged into every later LC/RC/QAM report). Only losing power
    clears it; the controller then returns still in TM mode and is switched
    back within TM_SWITCH_WINDOW_S without that leftover.
    """
    global _tm_power_cycled_at, _tm_power_cycles
    companion_common.require_supported_device("controller power cycle")
    _tm_power_cycles += 1
    ec_backend.set_controller_power(False)
    time.sleep(TM_POWER_OFF_S)
    ec_backend.set_controller_power(True)
    _tm_power_cycled_at = time.monotonic()


def _power_cycled_recently() -> bool:
    return _tm_power_cycled_at is not None and time.monotonic() - _tm_power_cycled_at < TM_SWITCH_WINDOW_S


def _consume_power_cycle() -> None:
    global _tm_power_cycled_at
    _tm_power_cycled_at = None


def power_cycles_exhausted() -> bool:
    """A firmware that will not leave TM mode must not lose power every half minute."""
    return _tm_power_cycles >= TM_POWER_CYCLE_LIMIT


def controller_settled() -> None:
    """Custom mode without a latched key: the next TM press gets fresh power cycles."""
    global _tm_power_cycles
    _tm_power_cycles = 0


EVIOCGKEY = 0x80604518  # _IOR('E', 0x18, 96): the keys an evdev node reports as held
ARROW_KEYS = (103, 105, 106, 108)  # KEY_UP, KEY_LEFT, KEY_RIGHT, KEY_DOWN


def latched_arrow_key() -> bool:
    """An arrow held on the controller's own keyboard interface.

    In custom mode the D-pad goes to the gamepad and this interface never sends
    arrows. Measured: a D-pad held while TM mode is left stays latched in the
    firmware and appears with the next LC/RC/LC1/RC1/QAM report; only a power
    cycle clears it, and a held D-pad cannot be seen beforehand.
    """
    for node in glob.glob("/dev/input/event*"):
        vendor, product, phys = _input_event_identity(node)
        if (vendor, product) != ("1c4f", "0002") or not phys.startswith(AYA3_CONTROLLER_PHYS_PREFIXES):
            continue
        import fcntl  # Linux only, like these nodes
        try:
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            keys = bytearray(96)
            fcntl.ioctl(fd, EVIOCGKEY, keys)
        except OSError:
            continue
        finally:
            os.close(fd)
        if any(keys[key // 8] >> (key % 8) & 1 for key in ARROW_KEYS):
            return True
    return False


def _reset_stale_tm_gamepad() -> bool:
    """Make the gamepad leave its TM-mode USB identity.

    Measured on the console: after AYA_CUSTOM from TM mode the firmware runs the
    custom mode but the USB gamepad stays 4001:0428 (the kernel then floods
    "input irq status -75" and there is no Xbox pad). A port reset makes the
    kernel see "device firmware changed" and enumerate it again as 045e:028e.
    """
    import errno
    for device in USB_DEVICES.glob("*"):
        try:
            if ((device / "idVendor").read_text().strip(), (device / "idProduct").read_text().strip()) != TM_GAMEPAD_ID:
                continue
            node = f"/dev/bus/usb/{int((device / 'busnum').read_text()):03d}/{int((device / 'devnum').read_text()):03d}"
        except (OSError, ValueError):
            continue
        import fcntl  # Linux only, like the device it resets
        fd = os.open(node, os.O_WRONLY)
        try:
            fcntl.ioctl(fd, USBDEVFS_RESET, 0)
        except OSError as error:
            if error.errno != errno.ENODEV:  # ENODEV: the reset already re-enumerated the gamepad
                raise
        finally:
            os.close(fd)
        decky.logger.info(f"{LOG} reset the gamepad out of its TM-mode USB identity")
        # The Xbox pad returns about 1.5 s later; FF_GAIN and the LC1/RC1 table
        # written right after this would otherwise fail and be skipped.
        deadline = time.monotonic() + TM_GAMEPAD_RETURN_S
        while time.monotonic() < deadline and not _rumble_event_node():
            time.sleep(0.1)
        return True
    return False


def read_module_layout() -> tuple[dict, dict]:
    path = vendor_hidraw()
    with controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            return decode_module_layout(_hid_exchange(fd, AYA_CHECK))
        finally:
            os.close(fd)


def program_rear_buttons(enabled: bool, config: dict | None = None) -> None:
    """Initialize AYANEO's button table and expose LC1/RC1 as L/R inputs."""
    if not companion_common.supported_device():
        raise RuntimeError("rear button setup is restricted to AYANEO 3")
    path = vendor_hidraw()
    with controller_apply_lock:
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
                current = normalize_controller(config)
                if not _hid_exchange(fd, controller_command(current, reset=True), timeout=1.5, attempts=1):
                    raise RuntimeError("controller rejected button-table activation reset")
                time.sleep(0.5)
                if not _hid_exchange(fd, controller_command(current)):
                    raise RuntimeError("controller rejected configuration restore")
            if not _hid_exchange(fd, AYA_SAVE, timeout=4.5, attempts=1):
                raise RuntimeError("controller did not save LC1/RC1 firmware bindings")
        finally:
            os.close(fd)


def read_controller() -> dict:
    with controller_apply_lock:
        path = vendor_hidraw()
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


def _write_verified_controller(fd, config: dict) -> None:
    """An ACK is insufficient: require a matching custom-mode state report."""
    command = controller_command(config)
    for _ in range(2):
        if not _hid_exchange(fd, command):
            raise RuntimeError("controller rejected configuration")
        report = _hid_exchange(fd, AYA_CHECK)
        if (controller_response_matches(report, config)
                and not controller_requires_custom(report)):
            return
    raise RuntimeError("controller did not confirm the requested configuration")


def apply_controller(config: dict, vibration_feedback: bool = False,
                     previous_vibration: str | None = None,
                     persist_firmware: bool = True, leave_tm: bool = True) -> bool:
    """Write the configuration; False when leave_tm=False found TM mode and wrote nothing."""
    companion_common.require_supported_device("controller configuration")
    config = normalize_controller(config)
    feedback_level = config["vibration"]
    with controller_apply_lock:
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
        path = vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            if not leave_tm and controller_requires_custom(check):
                return False  # TM Guard is off: the user's TM mode stays.
            _switch_to_custom_mode_fd(fd, check)
            _write_verified_controller(fd, config)
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
                if not _hid_exchange(fd, AYA_SAVE, timeout=4.5, attempts=1):
                    decky.logger.warning(
                        f"{LOG} controller configuration applied but firmware save was unanswered")
        finally:
            os.close(fd)
    return True


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
                         force: bool = False, repair: bool = True) -> tuple[str, str]:
    """Verify volatile controller state and repair it without writing NVRAM.

    With repair=False only an accidental TM mode change is undone; the
    controller's own lighting and vibration are left as they are.
    TM mode is left in two steps: "tm_power_cycle" asks the caller to cut the
    controller's power (power_cycle_controller); the controller returns still
    in TM mode and only then is switched back.
    """
    companion_common.require_supported_device("controller monitoring")
    config = normalize_controller(config)
    path = vendor_hidraw()
    with controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            response = _hid_exchange(fd, AYA_CHECK)
            if not response:
                raise RuntimeError("controller did not answer the state check")
            if controller_requires_custom(response):
                if not recover_custom:
                    return "tm_mode", path
                if not _power_cycled_recently():
                    return ("tm_stuck" if power_cycles_exhausted() else "tm_power_cycle"), path
                _switch_to_custom_mode_fd(fd, response)
                if not repair:
                    return "mode_restored", path
                status = "mode_restored"
            elif repair and (force or not controller_response_matches(response, config)):
                status = "configuration_restored"
            else:
                return "healthy", path
            _write_verified_controller(fd, config)
        finally:
            os.close(fd)
    # FF_GAIN belongs to the evdev node and is reset independently when that
    # node is recreated, so restore it together with the firmware packet.
    set_vibration_gain(config["ff_gain"])
    return status, path


def eject_controller_modules(side: str, config: dict) -> None:
    if side not in ("left", "right", "both"):
        raise ValueError("invalid controller module selection")
    if not companion_common.supported_device():
        raise RuntimeError("module eject is restricted to AYANEO 3")
    with controller_apply_lock:
        path = vendor_hidraw()
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        started = time.monotonic()
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            if not _hid_exchange(fd, controller_command(config, side), timeout=1.5, attempts=1):
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
        ec_backend.set_controller_power(False)


def reset_controller_modules(config: dict, restore_buttons: bool) -> None:
    """Re-initialize both Magic Modules and restore all volatile controller state."""
    if not companion_common.supported_device():
        raise RuntimeError("module reset is restricted to AYANEO 3")
    if not ec_backend.both_modules_connected():
        raise RuntimeError("insert both Magic Modules before resetting")
    if not ec_backend.controller_powered():
        ec_backend.set_controller_power(True)
        time.sleep(0.75)
    path = vendor_hidraw()
    with controller_apply_lock:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            check = _hid_exchange(fd, AYA_CHECK)
            _switch_to_custom_mode_fd(fd, check)
            if not _hid_exchange(fd, controller_command(config, reset=True), timeout=1.5, attempts=1):
                raise RuntimeError("controller rejected the Magic Module reset")
            time.sleep(0.5)
            if not _hid_exchange(fd, controller_command(config)):
                raise RuntimeError("controller did not return after the Magic Module reset")
        finally:
            os.close(fd)
    if restore_buttons:
        program_rear_buttons(True, config)
    apply_controller(config, persist_firmware=not restore_buttons)  # the button table already saved it
    set_vibration_gain(config["ff_gain"])



def _event_has_rumble(node: str) -> bool:
    try:
        import fcntl
        with open(node, "rb") as handle:
            bits = bytearray(32)
            fcntl.ioctl(handle.fileno(), EVIOCGBIT_FF, bits)
            return bool(bits[FF_RUMBLE // 8] & (1 << (FF_RUMBLE % 8)))
    except (ImportError, OSError):
        return False


INPUT_SYSFS = Path("/sys/class/input")
_event_identity_cache: dict[str, tuple[tuple[int, int], tuple[str, str, str]]] = {}


def _input_event_identity(node: str) -> tuple[str, str, str]:
    """Return the kernel vendor, product and physical path for an event node.

    Cached per device node: the guards ask every half second for every node,
    and a recreated node gets a new inode, so a stat is enough to notice it.
    """
    try:
        info = os.stat(node)
        key = (info.st_ino, getattr(info, "st_rdev", 0))
    except OSError:
        key = None
    cached = _event_identity_cache.get(node)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    device = INPUT_SYSFS / Path(node).name / "device"

    def read(name: str) -> str:
        try:
            return (device / name).read_text().strip().lower()
        except OSError:
            return ""

    identity = read("id/vendor"), read("id/product"), read("phys")
    if key is not None and identity[0]:  # a node still being created reads empty
        _event_identity_cache[node] = (key, identity)
    return identity


def _rumble_event_node() -> str | None:
    """Return AYANEO's physical gamepad without selecting an external pad."""
    nodes = sorted(glob.glob("/dev/input/event*"),
                   key=lambda path: int("".join(filter(str.isdigit, Path(path).name)) or 0))
    for node in nodes:
        vendor, product, phys = _input_event_identity(node)
        # The controller firmware exposes an Xbox 360-compatible xpad node
        # (045e:028e); development firmware can retain AYANEO's 1c4f VID.
        # Never select InputPlumber's 28de virtual controller, because FF_GAIN
        # must scale the physical device itself.
        if not ((vendor == "045e" and product == "028e") or vendor == "1c4f"):
            continue
        # VID/PID alone identifies Xbox-compatible hardware, not its owner.
        # Wait for a verified internal path instead of touching the sole
        # external pad while Magic Modules' evdev node is absent.
        if phys in AYA3_GAMEPAD_PHYS_PATHS and _event_has_rumble(node):
            return node
    return None


def device_node_token(path: str) -> tuple[str, int | None, int | None]:
    """Identify a device-node incarnation even when Linux reuses its name."""
    try:
        info = os.stat(path)
        return path, info.st_rdev, info.st_ino
    except OSError:
        return path, None, None


def rumble_event_token() -> tuple[str, int, int] | None:
    node = _rumble_event_node()
    if not node:
        return None
    try:
        info = os.stat(node)
        return node, info.st_rdev, info.st_ino
    except OSError:
        return None


def set_vibration_gain(percent: int) -> None:
    """Set Linux FF_GAIN on AYANEO's physical gamepad input device."""
    companion_common.require_supported_device("vibration control")
    gain = companion_common.clamp(percent, 0, 100)
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
    companion_common.require_supported_device("vibration control")
    import fcntl

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
