# SPDX-License-Identifier: BSD-3-Clause
"""Smart-amplifier tuning: aliases, calibration firmware, EFI and PipeWire idle sessions."""
import subprocess
import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
import main
import audio_backend


class AudioTests(unittest.TestCase):
    def test_background_tuning_never_stops_pipewire_while_sound_plays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "path"
            path.write_text("\n")
            with mock.patch.object(main.companion_common, "supported_device", return_value=True), \
                 mock.patch.object(audio_backend, "_prepare_audio_aliases"), \
                 mock.patch.object(audio_backend, "AUDIO_FIRMWARE_PATH", path), \
                 mock.patch.object(audio_backend, "_audio_card_index", return_value=0), \
                 mock.patch.object(audio_backend, "_audio_playback_active", return_value=True), \
                 mock.patch.object(audio_backend, "_set_audio_services") as services, \
                 mock.patch.object(audio_backend, "_reload_audio_dsps") as reload:
                with self.assertRaises(audio_backend.AudioBusy):
                    audio_backend.apply_audio_fix(stop_playback=False)
                services.assert_not_called(); reload.assert_not_called()
                self.assertEqual(path.read_text(), "\n", "nothing changes before the refusal")
                audio_backend.apply_audio_fix()  # the Audio page's Apply may stop it
                reload.assert_called_once()

    def test_tuning_error_names_the_reason_without_the_kernel_path(self):
        firmware = mock.Mock()
        firmware.read_text.side_effect = PermissionError(13, "Permission denied",
                                                         "/sys/module/firmware_class/parameters/path")
        with mock.patch.object(main.companion_common, "supported_device", return_value=True),              mock.patch.object(audio_backend, "_prepare_audio_aliases"),              mock.patch.object(audio_backend, "AUDIO_FIRMWARE_PATH", firmware):
            with self.assertRaises(RuntimeError) as raised:
                audio_backend.apply_audio_fix()
        self.assertEqual(str(raised.exception), "kernel firmware path is unavailable: Permission denied")
        self.assertIsInstance(raised.exception.__cause__, PermissionError, "the log keeps the whole error")

    def test_background_tuning_stops_retrying_once_an_apply_did_not_take(self):
        plugin = main.Plugin()
        state = {"audio_fix_enabled": True}
        with mock.patch.object(main.Plugin, "_state", state), \
             mock.patch.object(audio_backend, "audio_fix_ready", return_value=False), \
             mock.patch.object(audio_backend, "audio_fix_supported", return_value=True), \
             mock.patch.object(audio_backend, "apply_audio_fix") as apply:
            self.assertTrue(plugin._restore_audio_once(), "no endless minute-by-minute DSP reloads")
        apply.assert_called_once_with(stop_playback=False)
        self.assertIn("did not enter the tuned profile", state["audio_fix_error"])

    def test_calibration_i2c_bus_comes_from_sysfs_never_a_guess(self):
        # As on the console: /sys/bus/i2c/devices/i2c-CSC3551:00-cs35l41-hda.N -> .../AMDI0010:01/i2c-1/...
        class Devices:
            def __init__(self, *found):
                self.found = [Path(path) for path in found]
            def glob(self, pattern):
                self.pattern = pattern
                return self.found
        both = Devices("/sys/devices/platform/AMDI0010:01/i2c-1/amp0", "/sys/devices/platform/AMDI0010:01/i2c-1/amp1")
        with mock.patch.object(Path, "resolve", lambda path: path), \
             mock.patch.object(audio_backend, "AUDIO_AMPLIFIER_SYSFS", both):
            self.assertEqual(audio_backend._audio_i2c_bus(), "1")
            self.assertEqual(both.pattern, "i2c-CSC3551:00-cs35l41-hda.*")
            for devices, error in ((Devices(), "not found"),
                                   (Devices("/sys/x/i2c-1/amp0", "/sys/x/i2c-3/amp1"), "one I2C bus")):
                with self.subTest(error=error), \
                     mock.patch.object(audio_backend, "AUDIO_AMPLIFIER_SYSFS", devices), \
                     self.assertRaisesRegex(RuntimeError, error):
                    audio_backend._audio_i2c_bus()

    def test_audio_alias_names_preserve_compression(self):
        source = Path("cs35l41-dsp1-spk-prot-1f660105.bin.zst")
        self.assertEqual(audio_backend._audio_alias_filenames(source), (
            "cs35l41-dsp1-spk-prot-1f660105-spkid1-l0.bin.zst",
            "cs35l41-dsp1-spk-prot-1f660105-spkid1-r0.bin.zst",
        ))

    def test_prepare_audio_aliases_copies_current_system_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "system"
            source_dir.mkdir()
            source = source_dir / f"{audio_backend.AUDIO_FIRMWARE_STEM}.bin"
            source.write_bytes(b"ayaneo-tuning")
            old_system = audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR
            old_root = audio_backend.AUDIO_FIRMWARE_ROOT
            try:
                audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR = source_dir
                audio_backend.AUDIO_FIRMWARE_ROOT = root / "runtime"
                targets = audio_backend._prepare_audio_aliases()
            finally:
                audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR = old_system
                audio_backend.AUDIO_FIRMWARE_ROOT = old_root
            self.assertEqual([path.read_bytes() for path in targets], [b"ayaneo-tuning"] * 2)

    def test_audio_idle_session_stops_pipewire_when_suspend_keeps_pcm_open(self):
        calls = []
        with mock.patch.object(audio_backend, "_audio_playback_active", side_effect=[True, True]), \
             mock.patch.object(audio_backend, "_suspend_audio_outputs", return_value=["sink"]), \
             mock.patch.object(audio_backend, "_wait_for_audio_idle"), \
             mock.patch.object(audio_backend, "_resume_audio_outputs") as resume, \
             mock.patch.object(audio_backend, "_set_audio_services",
                               side_effect=lambda running: calls.append(running)):
            with audio_backend._audio_idle_session(1):
                calls.append("work")
        self.assertEqual(calls, [False, "work", True])
        resume.assert_called_once_with(["sink"])

    def test_prepare_audio_calibration_firmware_verifies_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = root / "system"
            system.mkdir()
            wmfw = system / f"{audio_backend.AUDIO_FIRMWARE_WMFW_STEM}.wmfw.zst"
            wmfw.write_bytes(b"ayaneo-wmfw")
            calibration = b"verified-calibration"
            old_system = audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR
            old_root = audio_backend.AUDIO_FIRMWARE_ROOT
            old_hash = audio_backend.AUDIO_CALIBRATION_SHA256
            try:
                audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR = system
                audio_backend.AUDIO_FIRMWARE_ROOT = root / "runtime"
                audio_backend.AUDIO_CALIBRATION_SHA256 = hashlib.sha256(calibration).hexdigest()
                with mock.patch.object(audio_backend, "_download_audio_calibration",
                                       return_value=calibration):
                    wmfw_target, bin_target = audio_backend._prepare_audio_calibration_firmware()
            finally:
                audio_backend.AUDIO_FIRMWARE_SYSTEM_DIR = old_system
                audio_backend.AUDIO_FIRMWARE_ROOT = old_root
                audio_backend.AUDIO_CALIBRATION_SHA256 = old_hash
            self.assertEqual(wmfw_target.read_bytes(), b"ayaneo-wmfw")
            self.assertEqual(bin_target.read_bytes(), calibration)

    def test_audio_efi_rebuild_preserves_amp_ids(self):
        payload = bytearray(audio_backend.AUDIO_EFI_PAYLOAD_SIZE)
        audio_backend.AUDIO_EFI_HEADER.pack_into(payload, 0, audio_backend.AUDIO_EFI_PAYLOAD_SIZE, 2)
        offset = audio_backend.AUDIO_EFI_HEADER.size
        audio_backend.AUDIO_EFI_RECORD.pack_into(payload, offset, 0x1111, 123, 21, 1, 11952)
        audio_backend.AUDIO_EFI_RECORD.pack_into(
            payload, offset + audio_backend.AUDIO_EFI_RECORD.size, 0x2222, 456, 22, 1, 11531)
        original = struct.pack("<I", audio_backend.AUDIO_EFI_ATTRIBUTES) + bytes(payload)
        candidate = audio_backend._build_audio_efi(original, (11956, 11477), 23)
        records = audio_backend._decode_audio_efi(candidate)
        self.assertEqual([record[0] for record in records], [0x1111, 0x2222])
        self.assertEqual([record[2] for record in records], [23, 23])
        self.assertEqual([record[4] for record in records], [11956, 11477])

    def test_audio_efi_write_is_one_operation_without_fsync(self):
        candidate = b"candidate"
        original = b"original!"
        path = mock.MagicMock()
        path.__str__.return_value = "/fake/efivar"
        path.read_bytes.return_value = candidate
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(subprocess, "run", return_value=completed), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "write", return_value=len(candidate)) as write, \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "fsync") as fsync:
            audio_backend._write_audio_efi(path, candidate, original)
        write.assert_called_once_with(7, candidate)
        fsync.assert_not_called()

    def test_audio_idle_session_resumes_sinks_when_pipewire_restart_fails(self):
        def services(running):
            if running:
                raise RuntimeError("restart failed")

        with mock.patch.object(audio_backend, "_audio_playback_active", side_effect=[True, True]), \
             mock.patch.object(audio_backend, "_suspend_audio_outputs", return_value=["sink"]), \
             mock.patch.object(audio_backend, "_wait_for_audio_idle"), \
             mock.patch.object(audio_backend, "_set_audio_services", side_effect=services), \
             mock.patch.object(audio_backend, "_resume_audio_outputs") as resume:
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                with audio_backend._audio_idle_session(1):
                    pass
        resume.assert_called_once_with(["sink"])

    def test_audio_firmware_type_recovers_both_dsps_after_partial_failure(self):
        loads = []
        with mock.patch.object(audio_backend, "_set_audio_firmware_load",
                               side_effect=lambda _card, control, enabled: loads.append((control, enabled))), \
             mock.patch.object(audio_backend, "_set_audio_control", side_effect=RuntimeError("type failed")), \
             mock.patch.object(main.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "type failed"):
                audio_backend._set_audio_firmware_type(1, 2)
        self.assertEqual(loads, [
            (audio_backend.AUDIO_FIRMWARE_CONTROLS[0], False),
            (audio_backend.AUDIO_FIRMWARE_CONTROLS[1], False),
            (audio_backend.AUDIO_FIRMWARE_CONTROLS[0], True),
            (audio_backend.AUDIO_FIRMWARE_CONTROLS[1], True),
        ])


if __name__ == "__main__":
    unittest.main()
