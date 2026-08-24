import asyncio, hashlib, json, os, struct, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main

class LogicTests(unittest.TestCase):
    def test_battery_status_uses_upower_time_to_full(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Charging", "capacity": "82",
                "energy_now": "37826000", "energy_full": "46130000",
                "power_now": "5139000", "voltage_now": "11550000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(main, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(main, "_upower_time_to_full", return_value=5869):
                value = main.battery_status()
        self.assertEqual(value["percent"], 82)
        self.assertEqual(value["status"], "Charging")
        self.assertEqual(value["seconds_to_full"], 5869)
        self.assertEqual(value["power_w"], 5.14)
        self.assertEqual(value["source"], "UPower")

    def test_battery_status_falls_back_to_energy_and_power(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Charging", "capacity": "82",
                "energy_now": "37826000", "energy_full": "46130000",
                "power_now": "5139000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(main, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(main, "_upower_time_to_full", return_value=None):
                value = main.battery_status()
        self.assertEqual(value["seconds_to_full"], 5817)
        self.assertEqual(value["source"], "sysfs")

    def test_battery_status_does_not_estimate_while_discharging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery", "status": "Discharging", "capacity": "70",
                "energy_now": "32000000", "energy_full": "46130000", "power_now": "8000000",
            }.items():
                (battery / name).write_text(value)
            with mock.patch.object(main, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(main, "_upower_time_to_full") as upower:
                value = main.battery_status()
        upower.assert_not_called()
        self.assertIsNone(value["seconds_to_full"])

    def test_charge_bypass_register_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io"
            path.write_bytes(bytes(256))
            old_path = main._ec_io_path
            old_ensure = main.ensure_charge_bypass_control
            old_supported = main.supported_device
            try:
                main._ec_io_path = lambda: path
                main.ensure_charge_bypass_control = lambda: path
                main.supported_device = lambda: True
                self.assertFalse(main.read_charge_bypass())
                main.write_charge_bypass(True)
                self.assertTrue(main.read_charge_bypass())
                self.assertEqual(path.read_bytes()[main.EC_CHARGE_REGISTER], main.EC_CHARGE_INHIBIT)
                main.write_charge_bypass(False)
                self.assertFalse(main.read_charge_bypass())
                self.assertEqual(path.read_bytes()[main.EC_CHARGE_REGISTER], main.EC_CHARGE_AUTO)
            finally:
                main._ec_io_path = old_path
                main.ensure_charge_bypass_control = old_ensure
                main.supported_device = old_supported

    def test_charge_bypass_prefers_kernel_charge_behaviour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            (battery / "type").write_text("Battery\n")
            behaviour = battery / "charge_behaviour"
            behaviour.write_text("[auto] inhibit-charge\n")
            with mock.patch.object(main, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(main, "supported_device", return_value=True), \
                 mock.patch.object(main, "_ec_io_path") as ec_path:
                self.assertEqual(main.ensure_charge_bypass_control(), behaviour)
                self.assertFalse(main.read_charge_bypass())
                main.write_charge_bypass(True)
                self.assertTrue(main.read_charge_bypass())
                main.write_charge_bypass(False)
                self.assertFalse(main.read_charge_bypass())
            ec_path.assert_not_called()

    def test_tdp_clamps_and_orders(self):
        self.assertEqual(main.normalize_tdp({"spl": 2, "sppt": 1, "fppt": 99}), {"spl": 5, "sppt": 5, "fppt": 37})
        self.assertEqual(main.normalize_tdp({"spl": 35, "sppt": 99, "fppt": 99}), {"spl": 35, "sppt": 37, "fppt": 37})
        self.assertEqual(main.PRESETS["Minimum"], {"spl": 5, "sppt": 8, "fppt": 10})
        self.assertEqual(main.PRESETS["Max"], {"spl": 32, "sppt": 35, "fppt": 37})
        self.assertEqual(main.tdp_preset(main.PRESETS["Balanced"]), "Balanced")
        self.assertEqual(main.tdp_preset(main.PRESETS["Balanced"], "Custom"), "Custom")

    def test_cpu_boost_sysfs_round_trip_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boost"
            path.write_text("1\n")
            previous_settings = dict(main.settings.data)
            previous_state = main.Plugin._state
            main.settings.data = {}
            main.Plugin._state = {"cpu_boost": True, "cpu_boost_supported": True}
            try:
                with mock.patch.object(main, "CPU_BOOST_PATH", path), \
                     mock.patch.object(main, "supported_device", return_value=True), \
                     mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(
                         return_value={"cpu_boost": False, "cpu_boost_supported": True})):
                    self.assertTrue(main.read_cpu_boost())
                    state = asyncio.run(main.Plugin().set_cpu_boost(False))
                    self.assertFalse(main.read_cpu_boost())
                    self.assertEqual(path.read_text(), "0")
                    self.assertFalse(main.settings.data["cpu_boost"])
                    self.assertFalse(state["cpu_boost"])
                    main.write_cpu_boost(True)
                    self.assertTrue(main.read_cpu_boost())
            finally:
                main.settings.data = previous_settings
                main.Plugin._state = previous_state

    def test_custom_game_profile_persists_exact_limits_and_is_reapplied(self):
        custom = {"spl": 17, "sppt": 23, "fppt": 31}
        previous_settings = dict(main.settings.data)
        previous_active_app = main.Plugin._active_app
        previous_state = main.Plugin._state
        main.settings.data = {"tdp": dict(main.DEFAULT_TDP)}
        main.Plugin._active_app = "480"
        main.Plugin._state = {"tdp": dict(main.DEFAULT_TDP)}
        try:
            with mock.patch.object(main, "apply_tdp") as apply, \
                 mock.patch.object(main, "supported_device", return_value=True), \
                 mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={"tdp": custom})):
                plugin = main.Plugin()
                asyncio.run(plugin.set_game_profile("480", custom, "Custom"))
                apply.assert_called_once_with(custom)
                self.assertEqual(main.settings.data["game_profiles"]["480"], {**custom, "preset": "Custom"})
                self.assertEqual(asyncio.run(plugin.get_game_profile("480")), {
                    "exists": True, "profile": custom, "preset": "Custom",
                })

                apply.reset_mock()
                main.Plugin._active_app = ""
                asyncio.run(plugin.set_active_app("480"))
                apply.assert_called_once_with(custom)
                self.assertEqual(main.Plugin._state["tdp"], custom)
                self.assertEqual(main.Plugin._state["tdp_preset"], "Custom")
        finally:
            main.settings.data = previous_settings
            main.Plugin._active_app = previous_active_app
            main.Plugin._state = previous_state

    def test_controller_normalization(self):
        value = main.normalize_controller({"vibration": "wat", "ff_gain": 999, "rgb_mode": "wat", "color": "oops", "brightness": 999})
        self.assertEqual(value, {"vibration": "high", "ff_gain": 100, "rgb_mode": "solid", "color": "6600ff", "brightness": 100})

    def test_ff_gain_event(self):
        writes = []
        with mock.patch.object(main, "_rumble_event_node", return_value="/dev/input/event5"), \
             mock.patch.object(main, "supported_device", return_value=True), \
             mock.patch.object(main.os, "open", return_value=7), \
             mock.patch.object(main.os, "write", side_effect=lambda _fd, data: writes.append(data) or len(data)), \
             mock.patch.object(main.os, "close"), \
             mock.patch.object(main.time, "time", return_value=12.25):
            main.set_vibration_gain(50)
        self.assertEqual(struct.unpack("<qqHHi", writes[0]),
                         (12, 250000, main.EV_FF, main.FF_GAIN, round(0xFFFF * 0.5)))

    def test_key_binding_map_preserves_native_buttons_and_adds_l5_r5(self):
        mapping = main.button_map_bytes().decode()
        self.assertIn("button: LeftTop", mapping)
        self.assertIn("button: RightTop", mapping)
        self.assertIn("button: LeftPaddle2", mapping)
        self.assertIn("button: RightPaddle2", mapping)
        self.assertIn("Quick Access legacy firmware fallback", mapping)
        self.assertIn("keyboard: KeyLeftMeta", mapping)
        self.assertIn("keyboard: KeyD", mapping)
        self.assertEqual(main.INPUT_MAP_TARGET.name, "ayaneo_type7.yaml")

    def test_rear_button_firmware_commands_only_target_lc1_rc1(self):
        left = main.rear_button_command(0x12, 0x0f)
        right = main.rear_button_command(0x13, 0x15)
        clear = main.rear_button_command(0x12, None)
        self.assertEqual(left[3:8], bytes([0x0b, 0x07, 0x12, 0, 0x02]))
        self.assertEqual(right[3:8], bytes([0x0b, 0x07, 0x13, 0, 0x02]))
        self.assertEqual((left[12], right[12]), (0x0f, 0x15))
        self.assertEqual(clear[3:8], bytes([0x0b, 0x07, 0x12, 0, 0]))
        self.assertEqual(int.from_bytes(left[1:3], "little"), sum(left[7:]))

    def test_complete_button_table_matches_known_ayaneo3_bindings(self):
        commands = main.button_table_commands()
        self.assertEqual(len(commands), 33)
        by_slot = {command[5]: command for command in commands}
        self.assertEqual((by_slot[0x10][12], by_slot[0x11][12]), (0x70, 0x71))
        self.assertEqual((by_slot[0x12][12], by_slot[0x13][12]), (0x0f, 0x15))
        self.assertEqual((by_slot[0x17][7], by_slot[0x17][10], by_slot[0x17][12]),
                         (0x02, 0x08, 0x07))

    def test_rear_button_programming_is_saved(self):
        calls = []

        def exchange(_fd, command, timeout=0.4):
            calls.append((command, timeout))
            return bytes(64)

        with mock.patch.object(main, "supported_device", return_value=True), \
             mock.patch.object(main, "_vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(main.os, "open", return_value=7), \
             mock.patch.object(main.os, "close"), \
             mock.patch.object(main.os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(main, "_hid_exchange", side_effect=exchange):
            main.program_rear_buttons(True)

        table = calls[1:34]
        self.assertEqual([command[5] for command, _ in table], list(range(33)))
        self.assertEqual([table[0x12][0][12], table[0x13][0][12]], [0x0f, 0x15])
        self.assertEqual(calls[-3][0][20], 0x88)
        self.assertEqual(calls[-2][0][20], 0x00)
        self.assertEqual(calls[-1], (main.AYA_SAVE, 1.5))

    def test_controller_packet_shape_and_checksum(self):
        packet = main.controller_command({"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50})
        self.assertEqual(len(packet), 65)
        self.assertEqual(packet[3:5], bytes([0x21, 0x09]))
        self.assertEqual(packet[24], 0x10)
        self.assertEqual(int.from_bytes(packet[1:3], "little"), sum(packet[7:]))

    def test_hid_exchange_retries_the_complete_command(self):
        command = main.AYA_CHECK
        response = bytearray(64)
        response[3] = command[4]
        with mock.patch.object(main.os, "write", return_value=len(command)) as write, \
             mock.patch.object(main.select, "select", side_effect=[([], [], []), ([7], [], [])]), \
             mock.patch.object(main.os, "read", return_value=bytes(response)):
            self.assertEqual(main._hid_exchange(7, command), bytes(response))
        self.assertEqual(write.call_count, 2)

    def test_hid_exchange_stops_after_three_unanswered_attempts(self):
        with mock.patch.object(main.os, "write", return_value=len(main.AYA_CHECK)) as write, \
             mock.patch.object(main.select, "select", return_value=([], [], [])):
            self.assertEqual(main._hid_exchange(7, main.AYA_CHECK), b"")
        self.assertEqual(write.call_count, main.HID_COMMAND_ATTEMPTS)

    def test_vendor_hidraw_rejects_active_kernel_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            hidraw = Path(directory) / "hidraw7"
            device = hidraw / "device"
            device.mkdir(parents=True)
            (device / "report_descriptor").write_bytes(b"\x06\x00\xff\x09\x01")
            (device / "uevent").write_text("HID_ID=0003:00001C4F:00000002\n")
            for name in ("module_left", "module_right", "eject", "reset"):
                (device / name).touch()
            with mock.patch.object(main.glob, "glob", return_value=[str(hidraw)]):
                with self.assertRaisesRegex(RuntimeError, "kernel hid-ayaneo is active"):
                    main._vendor_hidraw()

    def test_magic_module_eject_commands(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        self.assertEqual(main.controller_command(config, "left")[20], 0x07)
        self.assertEqual(main.controller_command(config, "right")[20], 0x70)
        self.assertEqual(main.controller_command(config, "both")[20], 0x77)

    def test_magic_module_eject_timeout_keeps_controller_powered(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        busy = bytearray(64)
        busy[19] = 0x02
        with mock.patch.object(main, "supported_device", return_value=True), \
             mock.patch.object(main, "_vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(main.os, "open", return_value=7), \
             mock.patch.object(main.os, "close"), \
             mock.patch.object(main.os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(main, "_hid_exchange", return_value=bytes(busy)), \
             mock.patch.object(main.time, "sleep"), \
             mock.patch.object(main, "set_controller_power") as set_power:
            with self.assertRaisesRegex(TimeoutError, "controller power was left on"):
                main.eject_controller_modules("both", config)
        set_power.assert_not_called()

    def test_magic_module_presence_matches_ec_absent_bits(self):
        with mock.patch.object(main, "_read_ec_register", return_value=0):
            self.assertEqual(main.module_presence(), {"left": True, "right": True})
        with mock.patch.object(main, "_read_ec_register", return_value=main.EC_MODULE_LEFT):
            self.assertEqual(main.module_presence(), {"left": False, "right": True})
        with mock.patch.object(main, "_read_ec_register", return_value=main.EC_MODULE_RIGHT):
            self.assertEqual(main.module_presence(), {"left": True, "right": False})

    def test_magic_module_identification_and_layout(self):
        response = bytearray(64)
        response[main.MODULE_INFO_LEFT_INDEX] = 0x44
        response[main.MODULE_INFO_RIGHT_INDEX] = 0x50
        left, right = main.decode_module_layout(bytes(response))
        self.assertEqual((left["label"], left["layout"]),
                         ("Joystick / Cross", "Top: Joystick · Bottom: Cross"))
        self.assertEqual((right["label"], right["layout"]),
                         ("Joystick / ABXY", "Top: ABXY · Bottom: Joystick"))

    def test_incomplete_module_pair_uses_unpowered_state(self):
        left, right = main.module_states_from_presence({"left": True, "right": False})
        self.assertEqual((left["status"], right["status"]), ("unpowered", "disconnected"))

    def test_tm_guard_switches_only_when_firmware_requests_custom_mode(self):
        needs_custom = bytearray(64)
        needs_custom[main.AYA_CUSTOM_REQUIRED_INDEX] = 1
        custom = bytearray(64)
        calls = []

        def exchange(_fd, command, timeout=0.4):
            calls.append(command)
            if command == main.AYA_CUSTOM:
                return bytes(64)
            return bytes(custom)

        with mock.patch.object(main, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(main.time, "sleep"):
            self.assertTrue(main._switch_to_custom_mode_fd(7, bytes(needs_custom)))
        self.assertIn(main.AYA_CUSTOM, calls)

        calls.clear()
        with mock.patch.object(main, "_hid_exchange", side_effect=exchange):
            self.assertFalse(main._switch_to_custom_mode_fd(7, bytes(custom)))
        self.assertNotIn(main.AYA_CUSTOM, calls)

    def test_magic_module_quick_reset_packet(self):
        config = {"vibration": "high", "rgb_mode": "solid", "color": "ff0000", "brightness": 100}
        self.assertEqual(main.controller_command(config, reset=True)[20], 0x88)

    def test_startup_restore_never_activates_magic_module_reset(self):
        previous = main.Plugin._state
        main.Plugin._state = {
            "tdp": dict(main.DEFAULT_TDP),
            "controller": dict(main.DEFAULT_CONTROLLER),
            "button_fix_installed": True,
        }
        try:
            with mock.patch.object(main.settings, "getSetting", return_value=None), \
                 mock.patch.object(main.asyncio, "sleep", new=mock.AsyncMock()), \
                 mock.patch.object(main, "apply_tdp"), \
                 mock.patch.object(main, "apply_controller"), \
                 mock.patch.object(main, "set_vibration_gain"), \
                 mock.patch.object(main, "program_rear_buttons") as program:
                asyncio.run(main.Plugin()._restore_hardware())
            program.assert_not_called()
        finally:
            main.Plugin._state = previous

    def test_off_packet(self):
        packet = main.controller_command({"vibration": "off", "rgb_mode": "off", "color": "ffffff", "brightness": 100})
        self.assertEqual(packet[8], 0xff)
        self.assertEqual(packet[12], 0xff)
        self.assertEqual(packet[24], 0x40)

    def test_vibration_feedback_is_single_pulse_without_firmware_save(self):
        config = {"vibration": "high", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        calls = []

        def exchange(_fd, command, timeout=0.4):
            calls.append(("hid", command[4], timeout))
            return bytes(64)

        with mock.patch.object(main, "_vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(main, "supported_device", return_value=True), \
             mock.patch.object(main.os, "open", return_value=7), \
             mock.patch.object(main.os, "close"), \
             mock.patch.object(main.os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(main, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(main, "play_vibration_test",
                               side_effect=lambda level, duration: calls.append(("pulse", level, duration))):
            main.apply_controller(config, True, "medium", False)
            config["vibration"] = "off"
            main.apply_controller(config, True, "high", False)

        self.assertEqual(calls[:3], [
            ("hid", 0x08, 0.4), ("hid", 0x09, 0.4),
            ("pulse", "high", main.VIBRATION_CONFIRM_MS),
        ])
        self.assertEqual(calls[3:6], [
            ("pulse", "high", main.VIBRATION_CONFIRM_MS), ("hid", 0x08, 0.4),
            ("hid", 0x09, 0.4),
        ])
        self.assertNotIn(("hid", 0x05, 1.5), calls)

    def test_download_url_allowlist(self):
        self.assertEqual(main._checked_download_url(main.RYZENADJ_URL), main.RYZENADJ_URL)
        with self.assertRaises(RuntimeError):
            main._checked_download_url("https://example.com/ryzenadj")

    def test_audio_alias_names_preserve_compression(self):
        source = Path("cs35l41-dsp1-spk-prot-1f660105.bin.zst")
        self.assertEqual(main._audio_alias_filenames(source), (
            "cs35l41-dsp1-spk-prot-1f660105-spkid1-l0.bin.zst",
            "cs35l41-dsp1-spk-prot-1f660105-spkid1-r0.bin.zst",
        ))

    def test_prepare_audio_aliases_copies_current_system_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "system"
            source_dir.mkdir()
            source = source_dir / f"{main.AUDIO_FIRMWARE_STEM}.bin"
            source.write_bytes(b"ayaneo-tuning")
            old_system = main.AUDIO_FIRMWARE_SYSTEM_DIR
            old_root = main.AUDIO_FIRMWARE_ROOT
            try:
                main.AUDIO_FIRMWARE_SYSTEM_DIR = source_dir
                main.AUDIO_FIRMWARE_ROOT = root / "runtime"
                targets = main._prepare_audio_aliases()
            finally:
                main.AUDIO_FIRMWARE_SYSTEM_DIR = old_system
                main.AUDIO_FIRMWARE_ROOT = old_root
            self.assertEqual([path.read_bytes() for path in targets], [b"ayaneo-tuning"] * 2)

    def test_audio_idle_session_stops_pipewire_when_suspend_keeps_pcm_open(self):
        calls = []
        with mock.patch.object(main, "_audio_playback_active", side_effect=[True, True]), \
             mock.patch.object(main, "_suspend_audio_outputs", return_value=["sink"]), \
             mock.patch.object(main, "_wait_for_audio_idle"), \
             mock.patch.object(main, "_resume_audio_outputs") as resume, \
             mock.patch.object(main, "_set_audio_services",
                               side_effect=lambda running: calls.append(running)):
            with main._audio_idle_session(1):
                calls.append("work")
        self.assertEqual(calls, [False, "work", True])
        resume.assert_called_once_with(["sink"])

    def test_prepare_audio_calibration_firmware_verifies_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = root / "system"
            system.mkdir()
            wmfw = system / f"{main.AUDIO_FIRMWARE_WMFW_STEM}.wmfw.zst"
            wmfw.write_bytes(b"ayaneo-wmfw")
            calibration = b"verified-calibration"
            old_system = main.AUDIO_FIRMWARE_SYSTEM_DIR
            old_root = main.AUDIO_FIRMWARE_ROOT
            old_hash = main.AUDIO_CALIBRATION_SHA256
            try:
                main.AUDIO_FIRMWARE_SYSTEM_DIR = system
                main.AUDIO_FIRMWARE_ROOT = root / "runtime"
                main.AUDIO_CALIBRATION_SHA256 = hashlib.sha256(calibration).hexdigest()
                with mock.patch.object(main, "_download_audio_calibration",
                                       return_value=calibration):
                    wmfw_target, bin_target = main._prepare_audio_calibration_firmware()
            finally:
                main.AUDIO_FIRMWARE_SYSTEM_DIR = old_system
                main.AUDIO_FIRMWARE_ROOT = old_root
                main.AUDIO_CALIBRATION_SHA256 = old_hash
            self.assertEqual(wmfw_target.read_bytes(), b"ayaneo-wmfw")
            self.assertEqual(bin_target.read_bytes(), calibration)

    def test_audio_efi_rebuild_preserves_amp_ids(self):
        payload = bytearray(main.AUDIO_EFI_PAYLOAD_SIZE)
        main.AUDIO_EFI_HEADER.pack_into(payload, 0, main.AUDIO_EFI_PAYLOAD_SIZE, 2)
        offset = main.AUDIO_EFI_HEADER.size
        main.AUDIO_EFI_RECORD.pack_into(payload, offset, 0x1111, 123, 21, 1, 11952)
        main.AUDIO_EFI_RECORD.pack_into(
            payload, offset + main.AUDIO_EFI_RECORD.size, 0x2222, 456, 22, 1, 11531)
        original = struct.pack("<I", main.AUDIO_EFI_ATTRIBUTES) + bytes(payload)
        candidate = main._build_audio_efi(original, (11956, 11477), 23)
        records = main._decode_audio_efi(candidate)
        self.assertEqual([record[0] for record in records], [0x1111, 0x2222])
        self.assertEqual([record[2] for record in records], [23, 23])
        self.assertEqual([record[4] for record in records], [11956, 11477])

    def test_audio_efi_write_is_one_operation_without_fsync(self):
        candidate = b"candidate"
        original = b"original!"
        path = mock.MagicMock()
        path.__str__.return_value = "/fake/efivar"
        path.read_bytes.return_value = candidate
        completed = main.subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(main.subprocess, "run", return_value=completed), \
             mock.patch.object(main.os, "open", return_value=7), \
             mock.patch.object(main.os, "write", return_value=len(candidate)) as write, \
             mock.patch.object(main.os, "close"), \
             mock.patch.object(main.os, "fsync") as fsync:
            main._write_audio_efi(path, candidate, original)
        write.assert_called_once_with(7, candidate)
        fsync.assert_not_called()

    def test_display_script_is_gamma22(self):
        script = main.LUA_SOURCE.read_text()
        self.assertTrue(main._is_our_display_script(main.LUA_SOURCE))
        self.assertIn("gamescope.eotf.gamma22", script)
        self.assertNotIn("gamescope.eotf.pq", script)
        self.assertIn("max_content_light_level = 800", script)
        self.assertIn("max_frame_average_luminance = 400", script)
        self.assertIn("r = { x = 0.6820, y = 0.3150 }", script)
        self.assertIn("g = { x = 0.2400, y = 0.7160 }", script)
        self.assertIn("b = { x = 0.1380, y = 0.0460 }", script)
        self.assertIn("w = { x = 0.3127, y = 0.3290 }", script)

    def test_ayaneo_edid_advertised_maxcll_patch(self):
        base = bytearray(128)
        base[:12] = b"\x00\xff\xff\xff\xff\xff\xff\x00\x07\x21\x13\x01"
        base[126] = 1
        base[127] = (-sum(base[:127])) & 0xff
        cta = bytearray(128)
        cta[0:3] = bytes([2, 3, 15])
        cta[4:8] = bytes([0xe3, 5, 128, 0])
        cta[8:15] = bytes([0xe6, 6, 5, 1, 138, 96, 7])
        cta[127] = (-sum(cta[:127])) & 0xff
        original = bytes(base + cta)
        patched = main.patch_ayaneo_edid(original)
        self.assertIsNotNone(patched)
        self.assertEqual(patched[140], 128)
        self.assertEqual(sum(patched[128:256]) & 0xff, 0)
        self.assertEqual(patched[:140], original[:140])
        self.assertEqual(patched[141:255], original[141:255])
        self.assertAlmostEqual(main._published_edid_nits(patched), 800.0)

    def test_edid_patch_rejects_other_display(self):
        self.assertIsNone(main.patch_ayaneo_edid(bytes(256)))

    def test_hidraw_response_layout(self):
        response = bytes.fromhex("0000000800000c016600ff016600ff00000002000033223000000000000000014450000064640000000000000000000000000000000000000000000000000000")
        old = main._hid_exchange
        old_path = main._vendor_hidraw
        old_open, old_close = main.os.open, main.os.close
        old_nonblock = getattr(main.os, "O_NONBLOCK", None)
        try:
            main._vendor_hidraw = lambda: "/dev/fake"
            main.os.open = lambda *_: 1
            main.os.close = lambda *_: None
            main.os.O_NONBLOCK = 0
            main._hid_exchange = lambda *_: response
            value = main.read_controller()
        finally:
            main._hid_exchange = old
            main._vendor_hidraw = old_path
            main.os.open, main.os.close = old_open, old_close
            if old_nonblock is None:
                del main.os.O_NONBLOCK
            else:
                main.os.O_NONBLOCK = old_nonblock
        self.assertEqual(value["vibration"], "high")
        self.assertEqual(value["rgb_mode"], "solid")
        self.assertEqual(value["color"], "3c00ff")

    def test_hx370_custom_mode_flag_uses_status_byte_18(self):
        response = bytes.fromhex(
            "0000000800000c019100ff019100ff000000010000332230000000000000000144"
            "5000006464000000000000000000000000000000000000000000000000000000")
        self.assertEqual(response[17], 0)
        self.assertEqual(response[18], 1)
        self.assertTrue(main.controller_requires_custom(response))

    def test_unsupported_device_blocks_privileged_hardware_writes(self):
        controller = dict(main.DEFAULT_CONTROLLER)
        operations = (
            (main._write_ec_register, (0x10, 0x01)),
            (main.apply_tdp, (main.DEFAULT_TDP,)),
            (main.write_cpu_boost, (False,)),
            (main.apply_controller, (controller,)),
            (main.set_vibration_gain, (50,)),
            (main.play_vibration_test, ("high", 100)),
            (main.write_charge_bypass, (True,)),
            (main.program_rear_buttons, (True, controller)),
            (main.install_display_script, ()),
            (main.install_button_fix, ()),
            (main.apply_audio_fix, ()),
            (main.remove_audio_fix, ()),
            (main.perform_audio_recalibration, ()),
        )
        with mock.patch.object(main, "supported_device", return_value=False), \
             mock.patch.object(main.subprocess, "run") as run:
            for operation, arguments in operations:
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(RuntimeError):
                        operation(*arguments)
        run.assert_not_called()

    def test_unload_does_not_probe_or_write_controller_after_device_mismatch(self):
        previous_state = main.Plugin._state
        task_names = (
            "_audio_task", "_restore_task", "_edid_task", "_ac_task",
            "_module_task", "_tm_guard_task",
        )
        previous_tasks = {name: getattr(main.Plugin, name) for name in task_names}
        main.Plugin._state = {"supported": True}
        for name in task_names:
            setattr(main.Plugin, name, None)
        try:
            with mock.patch.object(main, "supported_device", return_value=False), \
                 mock.patch.object(main, "controller_powered") as powered, \
                 mock.patch.object(main, "set_controller_power") as set_power:
                asyncio.run(main.Plugin()._unload())
            powered.assert_not_called()
            set_power.assert_not_called()
        finally:
            main.Plugin._state = previous_state
            for name, value in previous_tasks.items():
                setattr(main.Plugin, name, value)

    def test_unsupported_state_snapshot_skips_ayaneo_hardware_probes(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "supported": False,
            "tdp": dict(main.DEFAULT_TDP),
            "controller": dict(main.DEFAULT_CONTROLLER),
            "module_left": main._module_info("left"),
            "module_right": main._module_info("right"),
            "gpu_power_w": None,
            "charge_bypass_supported": False,
            "module_eject_supported": False,
            "module_reset_supported": False,
        }
        try:
            with mock.patch.object(main, "gpu_power_watts") as gpu, \
                 mock.patch.object(main, "read_charge_bypass") as charge, \
                 mock.patch.object(main, "module_presence") as modules:
                snapshot = main.Plugin._snapshot()
            self.assertFalse(snapshot["supported"])
            gpu.assert_not_called()
            charge.assert_not_called()
            modules.assert_not_called()
        finally:
            main.Plugin._state = previous_state

    def test_display_definition_upgrades_owned_legacy_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.lua"
            target = root / "target.lua"
            current = main.LUA_SOURCE.read_bytes()
            legacy = current.replace(main.DISPLAY_SCRIPT_MARKER, b"", 1)
            self.assertIn(hashlib.sha256(legacy).hexdigest(), main.LEGACY_DISPLAY_SCRIPT_SHA256)
            source.write_bytes(current)
            target.write_bytes(legacy)
            with mock.patch.object(main, "LUA_SOURCE", source), \
                 mock.patch.object(main, "LUA_TARGET", target), \
                 mock.patch.object(main, "supported_device", return_value=True):
                self.assertTrue(main._display_script_owned(target))
                main.install_display_script()
                self.assertEqual(target.read_bytes(), current)
                target.write_bytes(b"-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    main.install_display_script()
                self.assertEqual(target.read_bytes(), b"-- belongs to somebody else\n")

    def test_display_definition_removal_is_owned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "display.lua"
            target.write_bytes(main.LUA_SOURCE.read_bytes())
            with mock.patch.object(main, "LUA_TARGET", target), \
                 mock.patch.object(main, "supported_device", return_value=True):
                main.remove_display_script()
                self.assertFalse(target.exists())
                main.remove_display_script()
                target.write_text("-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    main.remove_display_script()
                self.assertTrue(target.exists())

    def test_display_switch_keeps_installed_definition_when_edid_retry_is_needed(self):
        state = {"screen_installed": True}
        with mock.patch.object(main, "install_display_script") as install_display, \
             mock.patch.object(main, "patch_published_edid", side_effect=OSError("busy")), \
             mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value=state)), \
             mock.patch.object(main.decky.logger, "warning") as warning:
            result = asyncio.run(main.Plugin().set_screen_fix(True))
        install_display.assert_called_once_with()
        warning.assert_called_once()
        self.assertIs(result, state)

    def test_input_map_upgrades_owned_legacy_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            target = root / "ayaneo_type7.yaml"
            current = main.INPUT_MAP_SOURCE.read_bytes()
            legacy = current.replace(main.INPUT_MAP_MARKER, b"", 1)
            self.assertIn(hashlib.sha256(legacy).hexdigest(), main.LEGACY_INPUT_MAP_SHA256)
            source.write_bytes(current)
            target.write_bytes(legacy)
            legacy_device = root / "legacy-device.yaml"
            legacy_map_a = root / "legacy-a.yaml"
            legacy_map_b = root / "legacy-b.yaml"
            with mock.patch.object(main, "INPUT_MAP_SOURCE", source), \
                 mock.patch.object(main, "INPUT_MAP_TARGET", target), \
                 mock.patch.object(main, "LEGACY_INPUT_DEVICE_TARGET", legacy_device), \
                 mock.patch.object(main, "LEGACY_INPUT_MAP_TARGETS", (legacy_map_a, legacy_map_b)), \
                 mock.patch.object(main, "supported_device", return_value=True):
                self.assertTrue(main.button_map_owned(target))
                main.install_button_fix()
                self.assertEqual(target.read_bytes(), current)
                target.write_bytes(b"name: External aya7 override\n")
                with self.assertRaises(RuntimeError):
                    main.install_button_fix()
                self.assertEqual(target.read_bytes(), b"name: External aya7 override\n")

    def test_audio_idle_session_resumes_sinks_when_pipewire_restart_fails(self):
        def services(running):
            if running:
                raise RuntimeError("restart failed")

        with mock.patch.object(main, "_audio_playback_active", side_effect=[True, True]), \
             mock.patch.object(main, "_suspend_audio_outputs", return_value=["sink"]), \
             mock.patch.object(main, "_wait_for_audio_idle"), \
             mock.patch.object(main, "_set_audio_services", side_effect=services), \
             mock.patch.object(main, "_resume_audio_outputs") as resume:
            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                with main._audio_idle_session(1):
                    pass
        resume.assert_called_once_with(["sink"])

    def test_audio_firmware_type_recovers_both_dsps_after_partial_failure(self):
        loads = []
        with mock.patch.object(main, "_set_audio_firmware_load",
                               side_effect=lambda _card, control, enabled: loads.append((control, enabled))), \
             mock.patch.object(main, "_set_audio_control", side_effect=RuntimeError("type failed")), \
             mock.patch.object(main.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "type failed"):
                main._set_audio_firmware_type(1, 2)
        self.assertEqual(loads, [
            (main.AUDIO_FIRMWARE_CONTROLS[0], False),
            (main.AUDIO_FIRMWARE_CONTROLS[1], False),
            (main.AUDIO_FIRMWARE_CONTROLS[0], True),
            (main.AUDIO_FIRMWARE_CONTROLS[1], True),
        ])

    def test_tdp_game_change_cannot_be_overwritten_by_delayed_profile_write(self):
        profile_a = {"spl": 17, "sppt": 22, "fppt": 29}
        profile_b = {"spl": 30, "sppt": 32, "fppt": 35, "preset": "Performance"}
        started = threading.Event()
        release = threading.Event()
        calls = []
        previous_settings = dict(main.settings.data)
        previous_active_app = main.Plugin._active_app
        previous_state = main.Plugin._state
        main.settings.data = {
            "tdp": dict(main.DEFAULT_TDP),
            "tdp_preset": "Balanced",
            "game_profiles": {"200": profile_b},
        }
        main.Plugin._active_app = "100"
        main.Plugin._state = {"tdp": dict(main.DEFAULT_TDP), "tdp_preset": "Balanced"}

        def apply(value):
            calls.append(dict(value))
            if value["spl"] == profile_a["spl"]:
                started.set()
                if not release.wait(2):
                    raise RuntimeError("test synchronization timed out")

        async def scenario():
            first = asyncio.create_task(
                main.Plugin().set_game_profile("100", profile_a, "Custom"))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(main.Plugin().set_active_app("200"))
            await asyncio.sleep(0.05)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)

        try:
            with mock.patch.object(main, "supported_device", return_value=True), \
                 mock.patch.object(main, "apply_tdp", side_effect=apply), \
                 mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={})):
                asyncio.run(scenario())
            self.assertEqual(calls, [profile_a, {"spl": 30, "sppt": 32, "fppt": 35}])
            self.assertEqual(main.Plugin._active_app, "200")
            self.assertEqual(main.Plugin._state["tdp"], {"spl": 30, "sppt": 32, "fppt": 35})
            self.assertEqual(main.Plugin._state["tdp_preset"], "Performance")
        finally:
            release.set()
            main.settings.data = previous_settings
            main.Plugin._active_app = previous_active_app
            main.Plugin._state = previous_state

    def test_updater_accepts_only_the_exact_release_asset(self):
        class Response:
            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return self.payload
            def geturl(self): return main.GITHUB_RELEASES_URL
            def close(self): pass

        release = {
            "tag_name": "v1.1.0",
            "assets": [{
                "name": "Ayaneo3Companion-1.1.0.zip",
                "browser_download_url": (
                    "https://github.com/Rayekkk/Ayaneo3Companion/releases/download/"
                    "v1.1.0/Ayaneo3Companion-1.1.0.zip"),
            }],
        }
        with mock.patch.object(main.updater, "open_url", return_value=Response(release)):
            info = main.updater.check()
        self.assertEqual(info["current_version"], "1.0.1")
        self.assertEqual(info["latest_version"], "1.1.0")
        self.assertTrue(info["update_available"])
        self.assertEqual(info["asset_name"], "Ayaneo3Companion-1.1.0.zip")

        release["assets"][0]["name"] = "source.zip"
        with mock.patch.object(main.updater, "open_url", return_value=Response(release)):
            info = main.updater.check()
        self.assertIn("Ayaneo3Companion-1.1.0.zip", info["error"])

    def test_release_version_and_package_inventory_agree(self):
        root = main.PLUGIN_DIR
        manifest = json.loads((root / "plugin.json").read_text())
        package = json.loads((root / "package.json").read_text())
        lock = json.loads((root / "package-lock.json").read_text())
        self.assertEqual(
            {manifest["version"], package["version"], lock["version"],
             lock["packages"][""]["version"]},
            {"1.0.1"},
        )
        package_script = (root / "scripts" / "package.mjs").read_text()
        self.assertIn('"lego_updater.py"', package_script)
        self.assertIn("THIRD_PARTY_LICENSES", package_script)
        self.assertIn("THIRD_PARTY_SOURCES", package_script)
        self.assertIn("Decky Loader API 1.1.3", (root / "NOTICE").read_text())

if __name__ == "__main__": unittest.main()
