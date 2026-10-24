# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk

"""AYANEO 3 smart amplifiers: CS35L41 tuning aliases, DSP reloads and EFI speaker calibration."""

import contextlib
import hashlib
import os
import shutil
import struct
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import decky

import companion_common
import system_process
from companion_common import LOG


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
AUDIO_CALIBRATION_BACKUP_ROOT = companion_common.BACKUP_ROOT / "audio-calibration"
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
AUDIO_USER_START_UNITS = (
    "pipewire.socket", "pipewire.service", "wireplumber.service",
    "pipewire-pulse.socket", "pipewire-pulse.service",
)
AUDIO_LEGACY_TEST_PATHS = frozenset({
    "/home/deck/ayaneo3-audio-cal-test",
    "/home/deck/ayaneo3-audio-fix",
})
_audio_apply_lock = threading.Lock()


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
        companion_common.atomic_write_bytes(target, source.read_bytes())
    return targets


def _audio_card_index() -> int | None:
    for card in range(8):
        try:
            result = system_process.run(["amixer", "-c", str(card), "controls"], 5,
                                        capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0 and all(name in result.stdout for name in AUDIO_FIRMWARE_CONTROLS):
            return card
    return None


def _set_audio_control(card: int, control: str, value, action: str = "set") -> None:
    result = system_process.run([
        "amixer", "-q", "-c", str(card), "cset",
        f"iface=CARD,name={control}", str(value),
    ], 15, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        if "resource busy" in message.lower():
            raise RuntimeError("audio is currently playing; stop playback and try again")
        raise RuntimeError(f"could not {action} {control[:2]} audio DSP: {message}")


def _audio_control_value(card: int, control: str) -> str | None:
    result = system_process.run([
        "amixer", "-c", str(card), "cget", f"iface=CARD,name={control}",
    ], 10, capture_output=True, text=True)
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
    account = companion_common.decky_account()
    uid = account.get("user_uid", -1)
    if type(uid) is not int or uid <= 0:
        raise RuntimeError("the Decky user account is unavailable for audio control")
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError) as error:
        raise RuntimeError("the Decky user account is unavailable for audio control") from error
    runtime = f"/run/user/{uid}"
    return system_process.run([
        "/usr/bin/runuser", "-u", name, "--", "/usr/bin/env",
        f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        *arguments,
    ], timeout, capture_output=True, text=True)


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


class AudioBusy(RuntimeError):
    """Sound is playing and the caller must not stop PipeWire for the DSP reload."""


def _apply_audio_fix_locked(stop_playback: bool = True) -> None:
    if not companion_common.supported_device():
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
    if not stop_playback and _audio_playback_active(card):
        # Background retries wait for silence: the reload would stop PipeWire mid-game.
        raise AudioBusy("audio is playing; speaker tuning waits until it stops")
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


def apply_audio_fix(stop_playback: bool = True) -> None:
    """stop_playback=False (background work) refuses with AudioBusy while sound plays."""
    companion_common.require_supported_device("audio tuning")
    with _audio_apply_lock:
        _apply_audio_fix_locked(stop_playback)


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
    companion_common.require_supported_device("audio tuning")
    with _audio_apply_lock:
        _remove_audio_fix_locked(reload_dsp)


def _download_audio_calibration() -> bytes:
    deadline = time.monotonic() + companion_common.HARDWARE_DOWNLOAD_SECONDS
    request = urllib.request.Request(
        companion_common.checked_download_url(AUDIO_CALIBRATION_URL),
        headers={"User-Agent": f"Ayaneo3Companion/{companion_common.updater.plugin_version()}"},
    )
    with companion_common.open_hardware_download(request) as response:
        companion_common.checked_download_url(response.geturl())
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > 64 * 1024:
            raise RuntimeError("audio calibration file is unexpectedly large")
        data = b"".join(companion_common.hardware_download_chunks(response, 64 * 1024, deadline))
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
        companion_common.atomic_write_bytes(wmfw_target, wmfw_source.read_bytes())

    bin_target = target_dir / AUDIO_CALIBRATION_BIN
    try:
        valid_bin = hashlib.sha256(bin_target.read_bytes()).hexdigest() == AUDIO_CALIBRATION_SHA256
    except OSError:
        valid_bin = False
    if not valid_bin:
        data = _download_audio_calibration()
        companion_common.atomic_write_bytes(bin_target, data)
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


AUDIO_AMPLIFIER_SYSFS = Path("/sys/bus/i2c/devices")


def _audio_i2c_bus() -> str:
    """The speaker amplifiers' I2C bus, from sysfs: the number depends on probe order.

    i2ctransfer runs with -f (past the bound driver), so a guessed bus could write
    the calibration into another device.
    """
    buses = {path.resolve().parent.name
             for path in AUDIO_AMPLIFIER_SYSFS.glob("i2c-CSC3551:00-cs35l41-hda.*")}
    if len(buses) != 1 or not next(iter(buses)).startswith("i2c-"):
        raise RuntimeError(f"AYANEO speaker amplifiers not found on one I2C bus: {sorted(buses)}")
    bus = next(iter(buses))[4:]
    if not bus.isdigit():
        raise RuntimeError(f"unexpected I2C bus for the speaker amplifiers: {bus}")
    return bus


def _i2c_register_arguments(address: str, register: int) -> list[str]:
    return [f"0x{(register >> shift) & 0xff:02x}" for shift in (24, 16, 8, 0)]


def _read_audio_dsp_register(address: str, register: int) -> int:
    result = system_process.run([
        "/usr/bin/i2ctransfer", "-f", "-y", _audio_i2c_bus(), f"w4@{address}",
        *_i2c_register_arguments(address, register), "r4",
    ], 5, capture_output=True, text=True)
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
    result = system_process.run([
        "/usr/bin/i2ctransfer", "-f", "-y", _audio_i2c_bus(), f"w8@{address}",
        *_i2c_register_arguments(address, register), *data,
    ], 5, capture_output=True, text=True)
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
    companion_common.private_dir(AUDIO_CALIBRATION_BACKUP_ROOT)
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

    unlocked = system_process.run(["/usr/bin/chattr", "-i", str(path)], 10, capture_output=True, text=True)
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
            locked = system_process.run(["/usr/bin/chattr", "+i", str(path)], 10,
                                        capture_output=True, text=True)
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
       env=system_process.system_env())
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
    companion_common.require_supported_device("audio recalibration")
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
