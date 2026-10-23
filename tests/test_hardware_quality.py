"""Failure-path tests; every device, sysfs path and command is isolated."""
import subprocess
import select
import urllib.request
import hashlib
import io
import os
import struct
import sys
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from _harness import install, owner
if "main" not in sys.modules:
    install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import display_backend
import audio_backend
import tdp_backend
import controller_backend
import ec_backend
import companion_common


class HardwareQualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for attribute, value in (("PUBLISHED_EDID", self.root / "edid.bin"),
                                 ("PUBLISHED_EDID_UID", 1000)):
            fixture = mock.patch.object(owner(attribute), attribute, value)
            fixture.start()
            self.addCleanup(fixture.stop)
        self.patches = context = mock.patch.object(companion_common, "supported_device", return_value=True)
        context.start()
        self.addCleanup(context.stop)
        command = mock.patch.object(subprocess, "run", side_effect=AssertionError("unexpected host command"))
        command.start()
        self.addCleanup(command.stop)

    def test_atomic_failure_keeps_original_and_removes_only_own_temporary(self):
        target = self.root / "config.yaml"
        target.write_bytes(b"original")
        old_temporary = self.root / ".config.yaml.tmp"
        old_temporary.write_bytes(b"unrelated")
        with mock.patch.object(os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                companion_common.atomic_write_bytes(target, b"replacement")
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(old_temporary.read_bytes(), b"unrelated")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [".config.yaml.tmp", "config.yaml"])

    def test_atomic_write_ignores_preexisting_temporary_symlink(self):
        target = self.root / "config.yaml"
        victim = self.root / "victim"
        victim.write_bytes(b"unrelated")
        link = self.root / ".config.yaml.tmp"
        try:
            link.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        companion_common.atomic_write_bytes(target, b"replacement")
        self.assertEqual(target.read_bytes(), b"replacement")
        self.assertEqual(victim.read_bytes(), b"unrelated")
        self.assertTrue(link.is_symlink())

    def audio_paths(self, configured=""):
        path = self.root / "firmware-path"
        path.write_text(configured)
        firmware = self.root / "firmware"
        firmware.mkdir()
        (firmware / "alias.bin").write_bytes(b"firmware")
        for name, value in (("AUDIO_FIRMWARE_PATH", path), ("AUDIO_FIRMWARE_ROOT", firmware)):
            patcher = mock.patch.object(owner(name), name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return path, firmware

    def test_audio_missing_card_does_not_change_firmware_path(self):
        path, _ = self.audio_paths()
        with mock.patch.object(audio_backend, "_prepare_audio_aliases"), \
             mock.patch.object(audio_backend, "_audio_card_index", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "controls were not found"):
                audio_backend.apply_audio_fix()
        self.assertEqual(path.read_text().strip(), "")

    def test_audio_enable_failure_restores_previous_path_and_reloads(self):
        path, _ = self.audio_paths()
        with mock.patch.object(audio_backend, "_prepare_audio_aliases"), \
             mock.patch.object(audio_backend, "_audio_card_index", return_value=1), \
             mock.patch.object(audio_backend, "_reload_audio_dsps", side_effect=[RuntimeError("reload failed"), None]) as reload:
            with self.assertRaisesRegex(RuntimeError, "reload failed"):
                audio_backend.apply_audio_fix()
        self.assertEqual(path.read_text().strip(), "")
        self.assertEqual(reload.call_args_list, [mock.call(1), mock.call(1)])

    def test_audio_disable_write_failure_preserves_firmware_and_path(self):
        path, firmware = self.audio_paths()
        path.write_text(str(firmware))
        original_write = Path.write_text

        def write(target, data, *args, **kwargs):
            if target == path and data == "\n":
                raise OSError("sysfs refused clear")
            return original_write(target, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write), \
             mock.patch.object(audio_backend, "_audio_card_index", return_value=1), \
             mock.patch.object(audio_backend, "_reload_audio_dsps"):
            with self.assertRaisesRegex(OSError, "sysfs refused clear"):
                audio_backend.remove_audio_fix()
        self.assertEqual(path.read_text().strip(), str(firmware))
        self.assertEqual((firmware / "alias.bin").read_bytes(), b"firmware")

    def test_audio_disable_unreadable_path_never_deletes_firmware(self):
        path, firmware = self.audio_paths()
        path.unlink()
        with self.assertRaisesRegex(RuntimeError, "safely read"):
            audio_backend.remove_audio_fix()
        self.assertTrue((firmware / "alias.bin").exists())

    def test_audio_disable_clears_sysfs_with_nonempty_write(self):
        path, firmware = self.audio_paths()
        path.write_text(str(firmware))
        original_write = Path.write_text
        written = []

        def write(target, data, *args, **kwargs):
            if target == path:
                written.append(data)
                if data == "":
                    return 0  # sysfs does not call its setter for zero bytes.
            return original_write(target, data, *args, **kwargs)

        with mock.patch.object(Path, "write_text", write):
            audio_backend.remove_audio_fix(reload_dsp=False)
        self.assertEqual(written, ["\n"])
        self.assertEqual(path.read_text().strip(), "")
        self.assertFalse(firmware.exists())

    def test_audio_disable_does_not_reload_an_unrelated_custom_path(self):
        path, firmware = self.audio_paths("/some/other/firmware")
        with mock.patch.object(audio_backend, "_reload_audio_dsps") as reload:
            audio_backend.remove_audio_fix()
        reload.assert_not_called()
        self.assertEqual(path.read_text(), "/some/other/firmware")
        self.assertFalse(firmware.exists())

    def test_audio_efi_failed_readback_still_attempts_and_verifies_rollback(self):
        path = self.root / "fake-efi"
        original, candidate = b"original", b"modified"
        writes = []
        with mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "write", side_effect=lambda _fd, data: writes.append(data) or len(data)), \
             mock.patch.object(Path, "read_bytes", side_effect=[OSError("read failed"), OSError("read failed"), original]), \
             mock.patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stderr="")) as command:
            with self.assertRaisesRegex(RuntimeError, "could not save audio calibration") as error:
                audio_backend._write_audio_efi(path, candidate, original)
        self.assertNotIn("recovery failed", str(error.exception))
        self.assertEqual(writes, [candidate, original])
        self.assertEqual([call.args[0][1] for call in command.call_args_list], ["-i", "+i"])

    def test_audio_efi_reports_failed_rollback_and_relock_together(self):
        path = self.root / "fake-efi"
        with mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "write", side_effect=lambda _fd, data: len(data)), \
             mock.patch.object(Path, "read_bytes", return_value=b"corrupted"), \
             mock.patch.object(subprocess, "run", side_effect=[
                 types.SimpleNamespace(returncode=0, stderr=""),
                 types.SimpleNamespace(returncode=1, stderr="relock failed")]):
            with self.assertRaises(RuntimeError) as error:
                audio_backend._write_audio_efi(path, b"modified", b"original")
        self.assertIn("recovery failed", str(error.exception))
        self.assertIn("could not relock", str(error.exception))

    def test_audio_measurement_failure_closes_playback_and_its_stderr(self):
        stderr = io.StringIO("playback output")
        playback = mock.Mock(stderr=stderr)
        playback.poll.side_effect = [None, 0]
        with mock.patch.object(audio_backend, "_write_audio_dsp_register"), \
             mock.patch.object(audio_backend, "_read_audio_dsp_register", side_effect=RuntimeError("read failed")), \
             mock.patch.object(subprocess, "Popen", return_value=playback):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                audio_backend._measure_audio_calibration(1)
        playback.terminate.assert_called_once()
        playback.wait.assert_called_once_with(timeout=2)
        self.assertTrue(stderr.closed)

    def tdp_writer(self):
        """tdp_backend with a verified helper whose runs are recorded instead of spawned."""
        runs = []
        for name, value in (("_supported_model", mock.Mock(return_value=True)),
                            ("_ryzenadj_available", True), ("_capture_baseline", mock.Mock()),
                            ("_helper_verified", mock.Mock(return_value=True)),
                            ("_spawn_ryzenadj", mock.Mock(side_effect=lambda args, timeout: (
                                runs.append(args) or (0, "", ""))))):
            patcher = mock.patch.object(tdp_backend, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return runs

    def test_powerstation_cannot_accept_any_three_limit_preset_or_bypass_its_writer(self):
        runs = self.tdp_writer()
        values = [*tdp_backend.PRESETS.values(), {"spl": 30, "sppt": 43, "fppt": 45}]
        with mock.patch.object(tdp_backend, "_powerstation_active", return_value=True):
            for value in values:
                with self.subTest(value=value):
                    result = tdp_backend._apply_limits(*(value[key] * 1000 for key in tdp_backend.LIMITS))
                    self.assertFalse(result["success"])
                    self.assertIn("PowerStation is active", result["stderr"])
        self.assertEqual(runs, [])

    def test_ryzenadj_receives_all_three_normalized_limits(self):
        runs = self.tdp_writer()
        with mock.patch.object(tdp_backend, "_powerstation_active", return_value=False):
            self.assertTrue(tdp_backend._apply_limits(35000, 999000, 999000)["success"])
        self.assertEqual(runs, [["--stapm-limit=30000", "--slow-limit=43000", "--fast-limit=45000"]])

    def test_powerstation_bus_owner_blocks_tdp_without_a_gpu_property_probe(self):
        with mock.patch.object(subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as command:
            self.assertTrue(tdp_backend._powerstation_active())
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0][2], "status")

    def test_powerstation_service_startup_and_reload_are_treated_as_active(self):
        for state, active in (("active", True), ("activating", True), ("reloading", True),
                              ("deactivating", True), ("inactive", False)):
            with self.subTest(state=state), mock.patch.object(subprocess, "run", side_effect=[
                    types.SimpleNamespace(returncode=1), types.SimpleNamespace(returncode=0, stdout=state)]):
                self.assertEqual(tdp_backend._powerstation_active(), active)

    def test_uncertain_powerstation_state_cannot_trigger_a_ryzenadj_write(self):
        runs = self.tdp_writer()
        with mock.patch.object(subprocess, "run", side_effect=[
                types.SimpleNamespace(returncode=1), types.SimpleNamespace(returncode=1, stdout="")]):
            result = tdp_backend._apply_limits(15000, 25000, 30000)
        self.assertIn("Could not verify", result["stderr"])
        self.assertEqual(runs, [])

    def test_kernel_charge_readback_failure_restores_original_value(self):
        path = self.root / "charge_behaviour"
        path.write_text("auto")
        with mock.patch.object(ec_backend, "ensure_charge_bypass_control", return_value=path), \
             mock.patch.object(ec_backend, "_charge_behaviour_path", return_value=path), \
             mock.patch.object(ec_backend, "_read_charge_behaviour", side_effect=[False, False, False]):
            with self.assertRaisesRegex(RuntimeError, "did not retain"):
                ec_backend.write_charge_bypass(True)
        self.assertEqual(path.read_text().strip(), "auto")

    def test_raw_charge_write_readback_is_one_ec_lock_transaction(self):
        path = self.root / "ec"
        data = bytearray(256)
        data[ec_backend.EC_CHARGE_REGISTER] = ec_backend.EC_CHARGE_AUTO
        path.write_bytes(data)
        with mock.patch.object(ec_backend, "ensure_charge_bypass_control", return_value=path), \
             mock.patch.object(ec_backend, "_charge_behaviour_path", return_value=None), \
             mock.patch.object(ec_backend, "read_charge_bypass", side_effect=AssertionError("unlocked second read")):
            ec_backend.write_charge_bypass(True)
        self.assertEqual(path.read_bytes()[ec_backend.EC_CHARGE_REGISTER], ec_backend.EC_CHARGE_INHIBIT)

    def test_download_redirect_is_rejected_before_redirect_request_is_created(self):
        handler = companion_common._HardwareDownloadRedirect()
        request = urllib.request.Request(tdp_backend.RYZENADJ_URL)
        with mock.patch.object(urllib.request.HTTPRedirectHandler, "redirect_request") as redirect:
            for url in ("http://github.com/file", "https://127.0.0.1/file", "https://github.com:8443/file",
                        "https://user:secret@github.com/file", "https://github.com.attacker.invalid/file"):
                with self.subTest(url=url), self.assertRaises(RuntimeError):
                    handler.redirect_request(request, None, 302, "Found", {}, url)
            redirect.assert_not_called()

    def test_hid_does_not_accept_matching_opcode_in_truncated_report(self):
        complete = bytearray(64)
        complete[3] = controller_backend.AYA_CHECK[4]
        with mock.patch.object(os, "write", return_value=len(controller_backend.AYA_CHECK)), \
             mock.patch.object(select, "select", return_value=([7], [], [])), \
             mock.patch.object(os, "read", side_effect=[bytes(complete[:4]), bytes(complete)]):
            self.assertEqual(controller_backend._hid_exchange(7, controller_backend.AYA_CHECK), bytes(complete))

    def test_vibration_interruption_stops_and_removes_uploaded_effect(self):
        calls = []

        def ioctl(fd, operation, argument):
            calls.append(operation)
            if operation == controller_backend.EVIOCSFF:
                struct.pack_into("<h", argument, 2, 3)

        with mock.patch.dict(sys.modules, {"fcntl": types.SimpleNamespace(ioctl=ioctl)}), \
             mock.patch.object(controller_backend, "_rumble_event_node", return_value="/fake/event"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "write", side_effect=lambda _fd, value: len(value)) as write, \
             mock.patch.object(os, "close") as close, \
             mock.patch.object(main.time, "sleep", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                controller_backend.play_vibration_test("medium")
        self.assertEqual(calls, [controller_backend.EVIOCSFF, controller_backend.EVIOCRMFF])
        self.assertEqual([struct.unpack("<qqHHi", call.args[1])[-1] for call in write.call_args_list], [1, 0])
        close.assert_called_once_with(7)

    def test_published_edid_rejects_out_of_range_cta_offset(self):
        data = bytearray(256)
        data[128] = 2
        data[130] = 255
        self.assertIsNone(display_backend.published_edid_nits(bytes(data)))

    @staticmethod
    def edid():
        data = bytearray(256)
        data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        data[8:12] = b"\x07\x21\x13\x01"
        data[126] = 1
        data[128:131] = bytes([2, 3, 9])
        data[132:137] = bytes([0xE4, 6, 4, 0, 32])
        data[127] = (-sum(data[:127])) & 255
        data[255] = (-sum(data[128:255])) & 255
        return bytes(data)

    def test_published_edid_bounds_read_before_touching_user_supplied_file(self):
        with mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close") as close, \
             mock.patch.object(os, "fstat", return_value=types.SimpleNamespace(
                 st_uid=1000, st_mode=0o100644, st_size=1024 * 1024 * 1024)), \
             mock.patch.object(os, "read") as read:
            self.assertFalse(display_backend.patch_published_edid())
        read.assert_not_called()
        close.assert_called_once_with(7)

    def test_published_edid_recovers_after_partial_write_error(self):
        path = self.root / "edid.bin"
        original = self.edid()
        path.write_bytes(original)
        real_write = os.write
        writes = 0

        def write(fd, value):
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(fd, value[:8])
            if writes == 2:
                raise OSError("interrupted write")
            return real_write(fd, value)

        with mock.patch.object(display_backend, "PUBLISHED_EDID", path), \
             mock.patch.object(os, "fstat", return_value=types.SimpleNamespace(
                 st_uid=1000, st_mode=0o100644, st_size=len(original))), \
             mock.patch.object(os, "write", side_effect=write):
            with self.assertRaisesRegex(OSError, "interrupted write"):
                display_backend.patch_published_edid()
        self.assertEqual(path.read_bytes(), original)

    def test_published_edid_status_bounds_reads_and_rejects_special_files(self):
        for mode, size in ((0o100644, 1024 * 1024 * 1024), (0o010600, 256)):
            with self.subTest(mode=mode, size=size), \
                 mock.patch.object(os, "open", return_value=7), \
                 mock.patch.object(os, "close") as close, \
                 mock.patch.object(os, "fstat", return_value=types.SimpleNamespace(
                     st_mode=mode, st_size=size)), \
                 mock.patch.object(os, "read") as read:
                self.assertEqual(display_backend.read_published_edid(), b"")
            read.assert_not_called()
            close.assert_called_once_with(7)

    def test_published_edid_status_reads_valid_copy(self):
        path = self.root / "edid.bin"
        original = self.edid()
        path.write_bytes(original)
        with mock.patch.object(display_backend, "PUBLISHED_EDID", path):
            self.assertEqual(display_backend.read_published_edid(), original)

    def test_audio_commands_use_the_actual_decky_account_and_bus(self):
        for name, uid in (("deck", 1000), ("gamer", 1001)):
            with self.subTest(name=name), \
                 mock.patch.object(companion_common, "decky_account", return_value={"user_uid": uid}), \
                 mock.patch.dict(sys.modules, {"pwd": types.SimpleNamespace(
                     getpwuid=lambda _uid: types.SimpleNamespace(pw_name=name))}), \
                 mock.patch.object(subprocess, "run") as run:
                audio_backend._deck_audio_command(["/usr/bin/pactl", "list", "short", "sinks"])
            command = run.call_args.args[0]
            self.assertEqual(command[:5], ["/usr/bin/runuser", "-u", name, "--", "/usr/bin/env"])
            self.assertIn(f"XDG_RUNTIME_DIR=/run/user/{uid}", command)
            self.assertIn(f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus", command)

    def test_missing_or_root_account_never_runs_audio_commands(self):
        for account in ({}, {"user_uid": 0}, {"user_uid": -1}):
            with self.subTest(account=account), \
                 mock.patch.object(companion_common, "decky_account", return_value=account), \
                 mock.patch.object(subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "Decky user account"):
                    audio_backend._deck_audio_command(["/usr/bin/pactl", "list", "short", "sinks"])
            run.assert_not_called()

    def test_published_edid_requires_decky_account_and_checks_its_uid(self):
        with mock.patch.object(display_backend, "PUBLISHED_EDID", None), \
             mock.patch.object(os, "open") as opened:
            self.assertEqual(display_backend.read_published_edid(), b"")
            self.assertFalse(display_backend.patch_published_edid())
        opened.assert_not_called()
        path = self.root / "edid.bin"
        original = self.edid()
        path.write_bytes(original)
        with mock.patch.object(display_backend, "PUBLISHED_EDID_UID", 1001), \
             mock.patch.object(os, "fstat", return_value=types.SimpleNamespace(
                 st_uid=1000, st_mode=0o100644, st_size=len(original))):
            self.assertFalse(display_backend.patch_published_edid())
        self.assertEqual(path.read_bytes(), original)
        with mock.patch.object(display_backend, "PUBLISHED_EDID_UID", 1001), \
             mock.patch.object(os, "fstat", return_value=types.SimpleNamespace(
                 st_uid=1001, st_mode=0o100644, st_size=len(original))):
            self.assertTrue(display_backend.patch_published_edid())
        self.assertEqual(round(display_backend.published_edid_nits(path.read_bytes())), display_backend.EDID_TARGET_NITS)


if __name__ == "__main__":
    unittest.main()
