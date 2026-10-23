import subprocess
import glob
import select
import asyncio, hashlib, io, json, os, struct, sys, tempfile, threading, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.dirname(__file__))
from _harness import install
install()
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import input_backend
import display_backend
import audio_backend
import power_backend
import controller_backend
import ec_backend
import companion_common

def controller_report(config):
    packet = controller_backend.controller_command(config)
    response = bytearray(64)
    response[3] = controller_backend.AYA_CHECK[4]
    response[7:15] = packet[8:16]
    response[21:24] = packet[22:25]
    return bytes(response)

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
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full", return_value=5869):
                value = ec_backend.battery_status()
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
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full", return_value=None):
                value = ec_backend.battery_status()
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
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(ec_backend, "_upower_time_to_full") as upower:
                value = ec_backend.battery_status()
        upower.assert_not_called()
        self.assertIsNone(value["seconds_to_full"])

    def test_charge_bypass_register_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "io"
            path.write_bytes(bytes(256))
            old_path = ec_backend._ec_io_path
            old_behaviour = ec_backend._charge_behaviour_path
            old_ensure = ec_backend.ensure_charge_bypass_control
            old_supported = companion_common.supported_device
            try:
                ec_backend._ec_io_path = lambda: path
                ec_backend._charge_behaviour_path = lambda: None
                ec_backend.ensure_charge_bypass_control = lambda: path
                companion_common.supported_device = lambda: True
                self.assertFalse(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(True)
                self.assertTrue(ec_backend.read_charge_bypass())
                self.assertEqual(path.read_bytes()[ec_backend.EC_CHARGE_REGISTER], ec_backend.EC_CHARGE_INHIBIT)
                ec_backend.write_charge_bypass(False)
                self.assertFalse(ec_backend.read_charge_bypass())
                self.assertEqual(path.read_bytes()[ec_backend.EC_CHARGE_REGISTER], ec_backend.EC_CHARGE_AUTO)
            finally:
                ec_backend._ec_io_path = old_path
                ec_backend._charge_behaviour_path = old_behaviour
                ec_backend.ensure_charge_bypass_control = old_ensure
                companion_common.supported_device = old_supported

    def test_charge_bypass_prefers_kernel_charge_behaviour(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            battery = root / "BAT0"
            battery.mkdir()
            (battery / "type").write_text("Battery\n")
            behaviour = battery / "charge_behaviour"
            behaviour.write_text("[auto] inhibit-charge\n")
            with mock.patch.object(ec_backend, "POWER_SUPPLY_ROOT", root), \
                 mock.patch.object(companion_common, "supported_device", return_value=True), \
                 mock.patch.object(ec_backend, "_ec_io_path") as ec_path:
                self.assertEqual(ec_backend.ensure_charge_bypass_control(), behaviour)
                self.assertFalse(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(True)
                self.assertTrue(ec_backend.read_charge_bypass())
                ec_backend.write_charge_bypass(False)
                self.assertFalse(ec_backend.read_charge_bypass())
            ec_path.assert_not_called()

    def test_tdp_clamps_and_orders(self):
        self.assertEqual(power_backend.normalize_tdp({"spl": 2, "sppt": 1, "fppt": 99}), {"spl": 5, "sppt": 5, "fppt": 37})
        self.assertEqual(power_backend.normalize_tdp({"spl": 35, "sppt": 99, "fppt": 99}), {"spl": 35, "sppt": 37, "fppt": 37})
        self.assertEqual(power_backend.PRESETS["Minimum"], {"spl": 5, "sppt": 8, "fppt": 10})
        self.assertEqual(power_backend.PRESETS["Max"], {"spl": 32, "sppt": 35, "fppt": 37})
        self.assertEqual(power_backend.tdp_preset(power_backend.PRESETS["Balanced"]), "Balanced")
        self.assertEqual(power_backend.tdp_preset(power_backend.PRESETS["Balanced"], "Custom"), "Custom")

    def test_cpu_boost_sysfs_round_trip_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boost"
            path.write_text("1\n")
            previous_settings = dict(main.settings.data)
            previous_state = main.Plugin._state
            main.settings.data = {}
            main.Plugin._state = {"cpu_boost": True, "cpu_boost_supported": True}
            try:
                with mock.patch.object(power_backend, "CPU_BOOST_PATH", path), \
                     mock.patch.object(companion_common, "supported_device", return_value=True), \
                     mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(
                         return_value={"cpu_boost": False, "cpu_boost_supported": True})):
                    self.assertTrue(power_backend.read_cpu_boost())
                    state = asyncio.run(main.Plugin().set_cpu_boost(False))
                    self.assertFalse(power_backend.read_cpu_boost())
                    self.assertEqual(path.read_text(), "0")
                    self.assertFalse(main.settings.data["cpu_boost"])
                    self.assertFalse(state["cpu_boost"])
                    power_backend.write_cpu_boost(True)
                    self.assertTrue(power_backend.read_cpu_boost())
            finally:
                main.settings.data = previous_settings
                main.Plugin._state = previous_state

    def test_custom_game_profile_persists_exact_limits_and_is_reapplied(self):
        custom = {"spl": 17, "sppt": 23, "fppt": 31}
        previous_settings = dict(main.settings.data)
        previous_active_app = main.Plugin._active_app
        previous_state = main.Plugin._state
        main.settings.data = {"tdp": dict(power_backend.DEFAULT_TDP)}
        main.Plugin._active_app = "480"
        main.Plugin._state = {"tdp": dict(power_backend.DEFAULT_TDP)}
        try:
            with mock.patch.object(power_backend, "apply_tdp") as apply, \
                 mock.patch.object(power_backend, "read_cpu_boost", return_value=True), \
                 mock.patch.object(power_backend, "write_cpu_boost"), \
                 mock.patch.object(companion_common, "supported_device", return_value=True), \
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
        value = controller_backend.normalize_controller({"vibration": "wat", "ff_gain": 999, "rgb_mode": "wat", "color": "oops", "brightness": 999})
        self.assertEqual(value, {"vibration": "high", "ff_gain": 100, "rgb_mode": "solid", "color": "6600ff", "brightness": 100})

    def test_ff_gain_event(self):
        writes = []
        with mock.patch.object(controller_backend, "_rumble_event_node", return_value="/dev/input/event5"), \
             mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "write", side_effect=lambda _fd, data: writes.append(data) or len(data)), \
             mock.patch.object(os, "close"), \
             mock.patch.object(main.time, "time", return_value=12.25):
            controller_backend.set_vibration_gain(50)
        self.assertEqual(struct.unpack("<qqHHi", writes[0]),
                         (12, 250000, controller_backend.EV_FF, controller_backend.FF_GAIN, round(0xFFFF * 0.5)))

    def test_key_binding_map_preserves_native_buttons_and_adds_l5_r5(self):
        mapping = input_backend.button_map_bytes().decode()
        self.assertIn("button: LeftTop", mapping)
        self.assertIn("button: RightTop", mapping)
        self.assertIn("button: LeftPaddle2", mapping)
        self.assertIn("button: RightPaddle2", mapping)
        self.assertIn("Quick Access legacy firmware fallback", mapping)
        self.assertIn("keyboard: KeyLeftMeta", mapping)
        self.assertIn("keyboard: KeyD", mapping)
        self.assertEqual(input_backend.INPUT_MAP_TARGET.name, "ayaneo_type7.yaml")

    def test_rear_button_firmware_commands_only_target_lc1_rc1(self):
        left = controller_backend.rear_button_command(0x12, 0x0f)
        right = controller_backend.rear_button_command(0x13, 0x15)
        clear = controller_backend.rear_button_command(0x12, None)
        self.assertEqual(left[3:8], bytes([0x0b, 0x07, 0x12, 0, 0x02]))
        self.assertEqual(right[3:8], bytes([0x0b, 0x07, 0x13, 0, 0x02]))
        self.assertEqual((left[12], right[12]), (0x0f, 0x15))
        self.assertEqual(clear[3:8], bytes([0x0b, 0x07, 0x12, 0, 0]))
        self.assertEqual(int.from_bytes(left[1:3], "little"), sum(left[7:]))

    def test_complete_button_table_matches_known_ayaneo3_bindings(self):
        commands = controller_backend.button_table_commands()
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

        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange):
            controller_backend.program_rear_buttons(True)

        table = calls[1:34]
        self.assertEqual([command[5] for command, _ in table], list(range(33)))
        self.assertEqual([table[0x12][0][12], table[0x13][0][12]], [0x0f, 0x15])
        self.assertEqual(calls[-3][0][20], 0x88)
        self.assertEqual(calls[-2][0][20], 0x00)
        self.assertEqual(calls[-1], (controller_backend.AYA_SAVE, 1.5))

    def test_controller_packet_shape_and_checksum(self):
        packet = controller_backend.controller_command({"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50})
        self.assertEqual(len(packet), 65)
        self.assertEqual(packet[3:5], bytes([0x21, 0x09]))
        self.assertEqual(packet[24], 0x10)
        self.assertEqual(int.from_bytes(packet[1:3], "little"), sum(packet[7:]))

    def test_controller_response_match_detects_rgb_and_vibration_drift(self):
        config = {
            "vibration": "low", "ff_gain": 70, "rgb_mode": "solid",
            "color": "ff0000", "brightness": 50,
        }
        packet = controller_backend.controller_command(config)
        response = bytearray(64)
        response[3] = controller_backend.AYA_CHECK[4]
        response[7:15] = packet[8:16]
        response[21:24] = packet[22:25]
        self.assertTrue(controller_backend.controller_response_matches(bytes(response), config))

        # One quantization step is tolerated, but a real colour or firmware
        # strength change must trigger an automatic restore.
        response[8] -= 1
        self.assertTrue(controller_backend.controller_response_matches(bytes(response), config))
        response[8] -= 1
        self.assertFalse(controller_backend.controller_response_matches(bytes(response), config))
        response[8:11] = packet[9:12]
        response[23] = controller_backend.VIBRATION_VALUES["high"] << 4
        self.assertFalse(controller_backend.controller_response_matches(bytes(response), config))

    def test_controller_reconcile_repairs_drift_without_firmware_save(self):
        config = {
            "vibration": "off", "ff_gain": 60, "rgb_mode": "solid",
            "color": "ff0000", "brightness": 50,
        }
        packet = controller_backend.controller_command(config)
        response = bytearray(64)
        response[3] = controller_backend.AYA_CHECK[4]
        response[7:15] = packet[8:16]
        response[21:24] = packet[22:25]
        response[23] = controller_backend.VIBRATION_VALUES["high"] << 4
        commands = []

        def exchange(_fd, command, timeout=0.4):
            commands.append((command, timeout))
            if command == packet:
                response[23] = packet[24]
            return bytes(response) if command == controller_backend.AYA_CHECK else bytes(64)

        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/hidraw7"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(controller_backend, "set_vibration_gain") as gain:
            status, path = controller_backend.reconcile_controller(config, True)

        self.assertEqual((status, path), ("configuration_restored", "/dev/hidraw7"))
        self.assertEqual([command for command, _ in commands], [controller_backend.AYA_CHECK, packet, controller_backend.AYA_CHECK])
        self.assertNotIn(controller_backend.AYA_SAVE, [command for command, _ in commands])
        gain.assert_called_once_with(60)

    def test_controller_reconcile_respects_disabled_tm_guard(self):
        config = dict(controller_backend.DEFAULT_CONTROLLER)
        response = bytearray(64)
        response[3] = controller_backend.AYA_CHECK[4]
        response[controller_backend.AYA_CUSTOM_REQUIRED_INDEX] = 1
        commands = []

        def exchange(_fd, command, timeout=0.4):
            commands.append(command)
            return bytes(response)

        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/hidraw7"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(controller_backend, "set_vibration_gain") as gain:
            status, _ = controller_backend.reconcile_controller(config, False, force=True)

        self.assertEqual(status, "tm_mode")
        self.assertEqual(commands, [controller_backend.AYA_CHECK])
        gain.assert_not_called()

    def test_hid_exchange_retries_the_complete_command(self):
        command = controller_backend.AYA_CHECK
        response = bytearray(64)
        response[3] = command[4]
        with mock.patch.object(os, "write", return_value=len(command)) as write, \
             mock.patch.object(select, "select", side_effect=[([], [], []), ([7], [], [])]), \
             mock.patch.object(os, "read", return_value=bytes(response)):
            self.assertEqual(controller_backend._hid_exchange(7, command), bytes(response))
        self.assertEqual(write.call_count, 2)

    def test_hid_exchange_stops_after_three_unanswered_attempts(self):
        with mock.patch.object(os, "write", return_value=len(controller_backend.AYA_CHECK)) as write, \
             mock.patch.object(select, "select", return_value=([], [], [])):
            self.assertEqual(controller_backend._hid_exchange(7, controller_backend.AYA_CHECK), b"")
        self.assertEqual(write.call_count, controller_backend.HID_COMMAND_ATTEMPTS)

    def test_vendor_hidraw_rejects_active_kernel_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            hidraw = Path(directory) / "hidraw7"
            device = hidraw / "device"
            device.mkdir(parents=True)
            (device / "report_descriptor").write_bytes(b"\x06\x00\xff\x09\x01")
            (device / "uevent").write_text("HID_ID=0003:00001C4F:00000002\n")
            for name in ("module_left", "module_right", "eject", "reset"):
                (device / name).touch()
            with mock.patch.object(glob, "glob", return_value=[str(hidraw)]):
                with self.assertRaisesRegex(RuntimeError, "kernel hid-ayaneo is active"):
                    controller_backend.vendor_hidraw()

    def test_magic_module_eject_commands(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        self.assertEqual(controller_backend.controller_command(config, "left")[20], 0x07)
        self.assertEqual(controller_backend.controller_command(config, "right")[20], 0x70)
        self.assertEqual(controller_backend.controller_command(config, "both")[20], 0x77)

    def test_magic_module_eject_timeout_keeps_controller_powered(self):
        config = {"vibration": "low", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        busy = bytearray(64)
        busy[19] = 0x02
        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", return_value=bytes(busy)), \
             mock.patch.object(main.time, "sleep"), \
             mock.patch.object(ec_backend, "set_controller_power") as set_power:
            with self.assertRaisesRegex(TimeoutError, "controller power was left on"):
                controller_backend.eject_controller_modules("both", config)
        set_power.assert_not_called()

    def test_module_eject_blocks_background_recovery_before_hardware_command(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": False,
            "modules_connected": True,
            "modules_detached": False,
            "module_left": controller_backend.module_info("left"),
            "module_right": controller_backend.module_info("right"),
        }

        def eject(_side, _controller):
            self.assertTrue(main.Plugin._state["modules_reconnecting"])
            self.assertFalse(main.Plugin._state["modules_connected"])
            self.assertEqual(main.Plugin._state["module_right"]["status"], "ejecting")

        try:
            with mock.patch.object(controller_backend, "eject_controller_modules", side_effect=eject), \
                 mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock(return_value={})):
                asyncio.run(main.Plugin().eject_modules("right"))
            self.assertTrue(main.Plugin._state["modules_reconnecting"])
        finally:
            main.Plugin._state = previous_state

    def test_module_monitor_does_not_undo_eject_poweroff_before_detach(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": True,
            "modules_connected": False,
            "modules_detached": False,
            "module_left": controller_backend.module_info("left", status="ejecting"),
            "module_right": controller_backend.module_info("right", status="ejecting"),
        }
        sleeps = 0

        async def stop_after_one_iteration(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        try:
            with mock.patch.object(main.asyncio, "sleep", side_effect=stop_after_one_iteration), \
                 mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
                 mock.patch.object(ec_backend, "controller_powered", return_value=False), \
                 mock.patch.object(ec_backend, "module_presence", return_value={"left": True, "right": True}), \
                 mock.patch.object(ec_backend, "set_controller_power") as set_power, \
                 mock.patch.object(controller_backend, "apply_controller") as apply:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._module_loop())
            set_power.assert_not_called()
            apply.assert_not_called()
        finally:
            main.Plugin._state = previous_state

    def test_magic_module_presence_matches_ec_absent_bits(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(ec_backend, "AYANEO_EC_ROOT", Path(directory) / "absent"):
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=0):
                self.assertEqual(ec_backend.module_presence(), {"left": True, "right": True})
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=ec_backend.EC_MODULE_LEFT):
                self.assertEqual(ec_backend.module_presence(), {"left": False, "right": True})
            with mock.patch.object(ec_backend, "_read_ec_register", return_value=ec_backend.EC_MODULE_RIGHT):
                self.assertEqual(ec_backend.module_presence(), {"left": True, "right": False})

    def test_magic_module_identification_and_layout(self):
        response = bytearray(64)
        response[controller_backend.MODULE_INFO_LEFT_INDEX] = 0x44
        response[controller_backend.MODULE_INFO_RIGHT_INDEX] = 0x50
        left, right = controller_backend.decode_module_layout(bytes(response))
        self.assertEqual((left["label"], left["layout"]),
                         ("Joystick / Cross", "Top: Joystick · Bottom: Cross"))
        self.assertEqual((right["label"], right["layout"]),
                         ("Joystick / ABXY", "Top: ABXY · Bottom: Joystick"))

    def test_incomplete_module_pair_uses_unpowered_state(self):
        left, right = controller_backend.module_states_from_presence({"left": True, "right": False})
        self.assertEqual((left["status"], right["status"]), ("unpowered", "disconnected"))

    def test_tm_guard_switches_only_when_firmware_requests_custom_mode(self):
        needs_custom = bytearray(64)
        needs_custom[controller_backend.AYA_CUSTOM_REQUIRED_INDEX] = 1
        custom = bytearray(64)
        calls = []

        def exchange(_fd, command, timeout=0.4):
            calls.append(command)
            if command == controller_backend.AYA_CUSTOM:
                return bytes(64)
            return bytes(custom)

        with mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(main.time, "sleep"):
            self.assertTrue(controller_backend._switch_to_custom_mode_fd(7, bytes(needs_custom)))
        self.assertIn(controller_backend.AYA_CUSTOM, calls)

        calls.clear()
        with mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange):
            self.assertFalse(controller_backend._switch_to_custom_mode_fd(7, bytes(custom)))
        self.assertNotIn(controller_backend.AYA_CUSTOM, calls)

    def test_controller_monitor_repairs_drift_even_when_tm_guard_is_disabled(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": False,
            "modules_connected": True,
            "tm_guard_enabled": False,
            "tm_guard_status": "Disabled",
            "tm_guard_recoveries": 0,
        }
        sleeps = 0

        async def stop_after_one_iteration(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                raise asyncio.CancelledError

        try:
            with mock.patch.object(main.asyncio, "sleep", side_effect=stop_after_one_iteration), \
                 mock.patch.object(controller_backend, "rumble_event_token", return_value=None), \
                 mock.patch.object(controller_backend, "device_node_token", return_value=("/dev/hidraw7", 1, 10)), \
                 mock.patch.object(controller_backend, "reconcile_controller",
                                   return_value=("configuration_restored", "/dev/hidraw7")) as reconcile:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._tm_guard_loop())
            reconcile.assert_called_once_with(controller_backend.DEFAULT_CONTROLLER, False, False, repair=True)
            self.assertEqual(main.Plugin._state["tm_guard_status"], "Disabled")
        finally:
            main.Plugin._state = previous_state

    def test_gain_monitor_restores_gain_when_event_device_is_recreated(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": {**controller_backend.DEFAULT_CONTROLLER, "ff_gain": 60},
            "modules_reconnecting": False,
            "modules_connected": True,
            "tm_guard_enabled": False,
            "tm_guard_status": "Disabled",
            "tm_guard_recoveries": 0,
        }
        sleeps = 0

        async def stop_after_two_iterations(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        try:
            with mock.patch.object(main.asyncio, "sleep",
                                   side_effect=stop_after_two_iterations), \
                 mock.patch.object(controller_backend, "reconcile_controller",
                                   return_value=("healthy", "/dev/hidraw7")), \
                 mock.patch.object(controller_backend, "device_node_token",
                                   return_value=("/dev/hidraw7", 1, 10)), \
                 mock.patch.object(controller_backend, "rumble_event_token", side_effect=[
                     ("/dev/input/event5", 2, 20),
                     ("/dev/input/event5", 2, 21),
                 ]), \
                 mock.patch.object(main, "CONTROLLER_GAIN_CHECK_INTERVAL", 0), \
                 mock.patch.object(controller_backend, "set_vibration_gain") as gain:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._vibration_gain_loop())
            self.assertEqual(gain.call_args_list, [mock.call(60), mock.call(60)])
        finally:
            main.Plugin._state = previous_state

    def test_controller_monitor_detects_reused_hidraw_name(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": False,
            "modules_connected": True,
            "tm_guard_enabled": False,
            "tm_guard_status": "Disabled",
            "tm_guard_recoveries": 0,
        }
        sleeps = 0

        async def stop_after_two_iterations(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        reconciliations = [
            ("healthy", "/dev/hidraw7"),
            ("healthy", "/dev/hidraw7"),
            ("configuration_restored", "/dev/hidraw7"),
        ]
        try:
            with mock.patch.object(main.asyncio, "sleep",
                                   side_effect=stop_after_two_iterations), \
                 mock.patch.object(controller_backend, "reconcile_controller",
                                   side_effect=reconciliations) as reconcile, \
                 mock.patch.object(controller_backend, "device_node_token", side_effect=[
                     ("/dev/hidraw7", 1, 10),
                     ("/dev/hidraw7", 1, 11),
                     ("/dev/hidraw7", 1, 11),
                 ]), \
                 mock.patch.object(controller_backend, "rumble_event_token", return_value=None), \
                 mock.patch.object(main, "CONTROLLER_GAIN_CHECK_INTERVAL", 0):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._tm_guard_loop())
            self.assertEqual(reconcile.call_args_list, [
                mock.call(controller_backend.DEFAULT_CONTROLLER, False, False, repair=True),
                mock.call(controller_backend.DEFAULT_CONTROLLER, False, False, repair=True),
                mock.call(controller_backend.DEFAULT_CONTROLLER, False, True, repair=True),
            ])
        finally:
            main.Plugin._state = previous_state

    def test_suspend_clock_offset_counts_only_boottime_difference(self):
        with mock.patch.object(main.time, "CLOCK_BOOTTIME", 7, create=True), \
             mock.patch.object(main.time, "clock_gettime", return_value=123.5,
                               create=True), \
             mock.patch.object(main.time, "monotonic", return_value=100.0):
            self.assertEqual(main._suspend_clock_offset(), 23.5)

    def test_controller_monitor_forces_one_restore_after_resume(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "modules_reconnecting": False,
            "modules_connected": True,
            "tm_guard_enabled": False,
            "tm_guard_status": "Disabled",
            "tm_guard_recoveries": 0,
        }
        sleeps = 0

        async def stop_after_two_iterations(_delay):
            nonlocal sleeps
            sleeps += 1
            if sleeps > 2:
                raise asyncio.CancelledError

        try:
            with mock.patch.object(main.asyncio, "sleep",
                                   side_effect=stop_after_two_iterations), \
                 mock.patch.object(main, "_suspend_clock_offset",
                                   side_effect=[10.0, 10.0, 15.0]), \
                 mock.patch.object(controller_backend, "reconcile_controller", side_effect=[
                     ("healthy", "/dev/hidraw7"),
                     ("configuration_restored", "/dev/hidraw7"),
                 ]) as reconcile, \
                 mock.patch.object(controller_backend, "device_node_token",
                                   return_value=("/dev/hidraw7", 1, 10)), \
                 mock.patch.object(controller_backend, "rumble_event_token", return_value=None), \
                 mock.patch.object(main, "CONTROLLER_GAIN_CHECK_INTERVAL", 0):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(main.Plugin()._tm_guard_loop())
            self.assertEqual(reconcile.call_args_list, [
                mock.call(controller_backend.DEFAULT_CONTROLLER, False, False, repair=True),
                mock.call(controller_backend.DEFAULT_CONTROLLER, False, True, repair=True),
            ])
        finally:
            main.Plugin._state = previous_state

    def test_magic_module_quick_reset_packet(self):
        config = {"vibration": "high", "rgb_mode": "solid", "color": "ff0000", "brightness": 100}
        self.assertEqual(controller_backend.controller_command(config, reset=True)[20], 0x88)

    def test_startup_restore_never_activates_magic_module_reset(self):
        previous = main.Plugin._state
        main.Plugin._state = {
            "tdp": dict(power_backend.DEFAULT_TDP),
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "button_fix_installed": True,
        }
        try:
            with mock.patch.object(main.settings, "getSetting", return_value=None), \
                 mock.patch.object(main.asyncio, "sleep", new=mock.AsyncMock()), \
                 mock.patch.object(power_backend, "apply_tdp"), \
                 mock.patch.object(controller_backend, "apply_controller"), \
                 mock.patch.object(controller_backend, "set_vibration_gain"), \
                 mock.patch.object(controller_backend, "program_rear_buttons") as program:
                asyncio.run(main.Plugin()._restore_hardware())
            program.assert_not_called()
        finally:
            main.Plugin._state = previous

    def test_off_packet(self):
        packet = controller_backend.controller_command({"vibration": "off", "rgb_mode": "off", "color": "ffffff", "brightness": 100})
        self.assertEqual(packet[8], 0xff)
        self.assertEqual(packet[12], 0xff)
        self.assertEqual(packet[24], 0x40)

    def test_vibration_feedback_is_single_pulse_without_firmware_save(self):
        config = {"vibration": "high", "rgb_mode": "solid", "color": "ff0000", "brightness": 50}
        calls = []

        def exchange(_fd, command, timeout=0.4):
            calls.append(("hid", command[4], timeout))
            return controller_report(config) if command == controller_backend.AYA_CHECK else bytes(64)

        with mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(controller_backend, "play_vibration_test",
                               side_effect=lambda level, duration: calls.append(("pulse", level, duration))):
            controller_backend.apply_controller(config, True, "medium", False)
            config["vibration"] = "off"
            controller_backend.apply_controller(config, True, "high", False)

        self.assertEqual(calls[:4], [
            ("hid", 0x08, 0.4), ("hid", 0x09, 0.4),
            ("hid", 0x08, 0.4),
            ("pulse", "high", controller_backend.VIBRATION_CONFIRM_MS),
        ])
        self.assertEqual(calls[4:8], [
            ("pulse", "high", controller_backend.VIBRATION_CONFIRM_MS), ("hid", 0x08, 0.4),
            ("hid", 0x09, 0.4), ("hid", 0x08, 0.4),
        ])
        self.assertNotIn(("hid", 0x05, 1.5), calls)

    def test_controller_write_retries_once_when_firmware_echo_is_stale(self):
        config = {
            "vibration": "off", "ff_gain": 100, "rgb_mode": "solid",
            "color": "ff0000", "brightness": 50,
        }
        packet = controller_backend.controller_command(config)
        stale = bytearray(64)
        stale[3] = controller_backend.AYA_CHECK[4]
        stale[7:15] = packet[8:16]
        stale[21:24] = packet[22:25]
        stale[23] = controller_backend.VIBRATION_VALUES["high"] << 4
        current = bytearray(stale)
        current[23] = packet[24]
        checks = iter((bytes(stale), bytes(stale), bytes(current)))
        config_writes = 0

        def exchange(_fd, command, timeout=0.4):
            nonlocal config_writes
            if command == controller_backend.AYA_CHECK:
                return next(checks)
            if command == packet:
                config_writes += 1
            return bytes(64)

        with mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange):
            controller_backend.apply_controller(config, persist_firmware=False)

        self.assertEqual(config_writes, 2)

    def test_failed_vibration_feedback_cannot_block_firmware_off(self):
        config = {
            "vibration": "off", "ff_gain": 100, "rgb_mode": "solid",
            "color": "ff0000", "brightness": 50,
        }
        commands = []

        def exchange(_fd, command, timeout=0.4):
            commands.append(command)
            return controller_report(config) if command == controller_backend.AYA_CHECK else bytes(64)

        with mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange), \
             mock.patch.object(controller_backend, "play_vibration_test", side_effect=OSError("not ready")):
            controller_backend.apply_controller(config, True, "high", False)

        self.assertEqual(commands[0], controller_backend.AYA_CHECK)
        self.assertEqual(commands[1][24], controller_backend.VIBRATION_VALUES["off"] << 4)

    def test_rumble_node_never_falls_back_to_virtual_controller(self):
        with mock.patch.object(glob, "glob", return_value=["/dev/input/event9"]), \
             mock.patch.object(controller_backend, "_event_has_rumble", return_value=True), \
             mock.patch.object(controller_backend, "_input_event_identity",
                               return_value=("28de", "1205", "virtual/input0")):
            self.assertIsNone(controller_backend._rumble_event_node())
        with mock.patch.object(glob, "glob", return_value=["/dev/input/event5"]), \
             mock.patch.object(controller_backend, "_event_has_rumble", return_value=True), \
             mock.patch.object(controller_backend, "_input_event_identity", return_value=(
                 "045e", "028e", "usb-0000:c4:00.3-2/input0")):
            self.assertEqual(controller_backend._rumble_event_node(), "/dev/input/event5")

    def test_rumble_node_prefers_ayaneo_phys_path_over_external_xbox_pad(self):
        identities = {
            "/dev/input/event3": ("045e", "028e", "usb-external/input0"),
            "/dev/input/event7": ("045e", "028e", "usb-0000:c7:00.0-2/input0"),
        }
        with mock.patch.object(glob, "glob", return_value=list(identities)), \
             mock.patch.object(controller_backend, "_event_has_rumble", return_value=True), \
             mock.patch.object(controller_backend, "_input_event_identity",
                               side_effect=lambda node: identities[node]):
            self.assertEqual(controller_backend._rumble_event_node(), "/dev/input/event7")

    def test_rumble_node_rejects_multiple_ambiguous_hardware_candidates(self):
        identities = {
            "/dev/input/event3": ("045e", "028e", "usb-external-a/input0"),
            "/dev/input/event7": ("045e", "028e", "usb-external-b/input0"),
        }
        with mock.patch.object(glob, "glob", return_value=list(identities)), \
             mock.patch.object(controller_backend, "_event_has_rumble", return_value=True), \
             mock.patch.object(controller_backend, "_input_event_identity",
                               side_effect=lambda node: identities[node]):
            self.assertIsNone(controller_backend._rumble_event_node())

    def test_download_url_allowlist(self):
        self.assertEqual(companion_common.checked_download_url(power_backend.RYZENADJ_URL), power_backend.RYZENADJ_URL)
        with self.assertRaises(RuntimeError):
            companion_common.checked_download_url("https://example.com/ryzenadj")

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

    def test_display_script_is_gamma22(self):
        script = display_backend.LUA_SOURCE.read_text()
        self.assertTrue(display_backend.is_our_display_script(display_backend.LUA_SOURCE))
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
        patched = display_backend.patch_ayaneo_edid(original)
        self.assertIsNotNone(patched)
        self.assertEqual(patched[140], 128)
        self.assertEqual(sum(patched[128:256]) & 0xff, 0)
        self.assertEqual(patched[:140], original[:140])
        self.assertEqual(patched[141:255], original[141:255])
        self.assertAlmostEqual(display_backend.published_edid_nits(patched), 800.0)

    def test_edid_patch_rejects_other_display(self):
        self.assertIsNone(display_backend.patch_ayaneo_edid(bytes(256)))

    def test_hidraw_response_layout(self):
        response = bytes.fromhex("0000000800000c016600ff016600ff00000002000033223000000000000000014450000064640000000000000000000000000000000000000000000000000000")
        old = controller_backend._hid_exchange
        old_path = controller_backend.vendor_hidraw
        old_open, old_close = os.open, os.close
        old_nonblock = getattr(os, "O_NONBLOCK", None)
        try:
            controller_backend.vendor_hidraw = lambda: "/dev/fake"
            os.open = lambda *_: 1
            os.close = lambda *_: None
            os.O_NONBLOCK = 0
            controller_backend._hid_exchange = lambda *_: response
            value = controller_backend.read_controller()
        finally:
            controller_backend._hid_exchange = old
            controller_backend.vendor_hidraw = old_path
            os.open, os.close = old_open, old_close
            if old_nonblock is None:
                del os.O_NONBLOCK
            else:
                os.O_NONBLOCK = old_nonblock
        self.assertEqual(value["vibration"], "high")
        self.assertEqual(value["rgb_mode"], "solid")
        self.assertEqual(value["color"], "3c00ff")
        self.assertTrue(controller_backend.controller_response_matches(response, value))

    def test_hx370_custom_mode_flag_uses_status_byte_18(self):
        response = bytes.fromhex(
            "0000000800000c019100ff019100ff000000010000332230000000000000000144"
            "5000006464000000000000000000000000000000000000000000000000000000")
        self.assertEqual(response[17], 0)
        self.assertEqual(response[18], 1)
        self.assertTrue(controller_backend.controller_requires_custom(response))

    def test_unsupported_device_blocks_privileged_hardware_writes(self):
        controller = dict(controller_backend.DEFAULT_CONTROLLER)
        operations = (
            (ec_backend._write_ec_register, (0x10, 0x01)),
            (power_backend.apply_tdp, (power_backend.DEFAULT_TDP,)),
            (power_backend.write_cpu_boost, (False,)),
            (controller_backend.apply_controller, (controller,)),
            (controller_backend.set_vibration_gain, (50,)),
            (controller_backend.play_vibration_test, ("high", 100)),
            (ec_backend.write_charge_bypass, (True,)),
            (controller_backend.program_rear_buttons, (True, controller)),
            (display_backend.install_display_script, ()),
            (input_backend.install_button_fix, ()),
            (audio_backend.apply_audio_fix, ()),
            (audio_backend.remove_audio_fix, ()),
            (audio_backend.perform_audio_recalibration, ()),
        )
        with mock.patch.object(companion_common, "supported_device", return_value=False), \
             mock.patch.object(subprocess, "run") as run:
            for operation, arguments in operations:
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(RuntimeError):
                        operation(*arguments)
        run.assert_not_called()

    def test_unload_does_not_probe_or_write_controller_after_device_mismatch(self):
        previous_state = main.Plugin._state
        task_names = (
            "_audio_task", "_restore_task", "_edid_task", "_ac_task", "_charge_task",
            "_module_task", "_tm_guard_task", "_vibration_gain_task",
        )
        previous_tasks = {name: getattr(main.Plugin, name) for name in task_names}
        main.Plugin._state = {"supported": True}
        for name in task_names:
            setattr(main.Plugin, name, None)
        try:
            with mock.patch.object(companion_common, "supported_device", return_value=False), \
                 mock.patch.object(ec_backend, "controller_powered") as powered, \
                 mock.patch.object(ec_backend, "set_controller_power") as set_power:
                asyncio.run(main.Plugin()._unload())
            powered.assert_not_called()
            set_power.assert_not_called()
        finally:
            main.Plugin._state = previous_state
            for name, value in previous_tasks.items():
                setattr(main.Plugin, name, value)

    def test_unload_does_not_undo_an_in_progress_module_eject(self):
        previous_state = main.Plugin._state
        task_names = (
            "_audio_task", "_restore_task", "_edid_task", "_ac_task", "_charge_task",
            "_module_task", "_tm_guard_task", "_vibration_gain_task",
        )
        previous_tasks = {name: getattr(main.Plugin, name) for name in task_names}
        main.Plugin._state = {"supported": True, "modules_reconnecting": True}
        for name in task_names:
            setattr(main.Plugin, name, None)
        try:
            with mock.patch.object(companion_common, "supported_device") as supported, \
                 mock.patch.object(ec_backend, "both_modules_connected") as connected, \
                 mock.patch.object(ec_backend, "controller_powered") as powered, \
                 mock.patch.object(ec_backend, "set_controller_power") as set_power:
                asyncio.run(main.Plugin()._unload())
            supported.assert_not_called()
            connected.assert_not_called()
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
            "tdp": dict(power_backend.DEFAULT_TDP),
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "module_left": controller_backend.module_info("left"),
            "module_right": controller_backend.module_info("right"),
            "gpu_power_w": None,
            "charge_bypass_supported": False,
            "module_eject_supported": False,
            "module_reset_supported": False,
        }
        try:
            with mock.patch.object(power_backend, "gpu_power_watts") as gpu, \
                 mock.patch.object(ec_backend, "read_charge_bypass") as charge, \
                 mock.patch.object(ec_backend, "module_presence") as modules:
                snapshot = main.Plugin._snapshot()
            self.assertFalse(snapshot["supported"])
            gpu.assert_not_called()
            charge.assert_not_called()
            modules.assert_not_called()
        finally:
            main.Plugin._state = previous_state

    def test_display_definition_upgrades_owned_legacy_file_only(self):
        self.assertTrue({
            "98de33235379f187b9d0eeb1460016b71fbc808c7cecff574774bbae64a130d4",
            "f52b721078df6336855c543da325a908477382cd8f313d395a7eabf1ad9f0b21",
        }.issubset(display_backend.LEGACY_DISPLAY_SCRIPT_SHA256))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.lua"
            target = root / "target.lua"
            current = display_backend.LUA_SOURCE.read_bytes()
            legacy = current.replace(display_backend.DISPLAY_SCRIPT_MARKER, b"", 1)
            self.assertIn(hashlib.sha256(legacy).hexdigest(), display_backend.LEGACY_DISPLAY_SCRIPT_SHA256)
            source.write_bytes(current)
            target.write_bytes(legacy)
            with mock.patch.object(display_backend, "LUA_SOURCE", source), \
                 mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                self.assertTrue(display_backend.display_script_owned(target))
                display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), current)
                target.write_bytes(b"-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), b"-- belongs to somebody else\n")

    def test_display_definition_replacement_requires_confirmation_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.lua"
            target = root / "target.lua"
            backups = root / "backups"
            current = display_backend.LUA_SOURCE.read_bytes()
            foreign = b"-- existing custom display definition\n"
            source.write_bytes(current)
            target.write_bytes(foreign)
            with mock.patch.object(display_backend, "LUA_SOURCE", source), \
                 mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(display_backend, "DISPLAY_SCRIPT_BACKUP_ROOT", backups), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                self.assertTrue(display_backend.display_script_conflict(target))
                with self.assertRaisesRegex(RuntimeError, "requires confirmation"):
                    display_backend.install_display_script()
                self.assertEqual(target.read_bytes(), foreign)
                backup = display_backend.install_display_script(replace_existing=True)
                self.assertIsNotNone(backup)
                self.assertEqual(backup.read_bytes(), foreign)
                self.assertEqual(target.read_bytes(), current)
                self.assertFalse(display_backend.display_script_conflict(target))

    def test_display_definition_removal_is_owned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "display.lua"
            target.write_bytes(display_backend.LUA_SOURCE.read_bytes())
            with mock.patch.object(display_backend, "LUA_TARGET", target), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                display_backend.remove_display_script()
                self.assertFalse(target.exists())
                display_backend.remove_display_script()
                target.write_text("-- belongs to somebody else\n")
                with self.assertRaises(RuntimeError):
                    display_backend.remove_display_script()
                self.assertTrue(target.exists())

    def test_display_switch_keeps_installed_definition_when_edid_retry_is_needed(self):
        with mock.patch.object(display_backend, "install_display_script") as install_display, \
             mock.patch.object(display_backend, "patch_published_edid", side_effect=OSError("busy")), \
             mock.patch.object(main.Plugin, "get_state", new=mock.AsyncMock()) as diagnostics, \
             mock.patch.object(main.decky.logger, "warning") as warning:
            result = asyncio.run(main.Plugin().set_screen_fix(True))
        install_display.assert_called_once_with(False)
        warning.assert_called_once()
        self.assertTrue(result["screen_installed"])
        self.assertTrue(main.Plugin._state["screen_installed"])
        diagnostics.assert_not_called()

    def test_input_map_upgrades_owned_legacy_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            target = root / "ayaneo_type7.yaml"
            current = input_backend.INPUT_MAP_SOURCE.read_bytes()
            legacy = current.replace(input_backend.INPUT_MAP_MARKER, b"", 1)
            self.assertIn(hashlib.sha256(legacy).hexdigest(), input_backend.LEGACY_INPUT_MAP_SHA256)
            source.write_bytes(current)
            target.write_bytes(legacy)
            legacy_device = root / "legacy-device.yaml"
            legacy_map_a = root / "legacy-a.yaml"
            legacy_map_b = root / "legacy-b.yaml"
            with mock.patch.object(input_backend, "INPUT_MAP_SOURCE", source), \
                 mock.patch.object(input_backend, "INPUT_MAP_TARGET", target), \
                 mock.patch.object(input_backend, "LEGACY_INPUT_DEVICE_TARGET", legacy_device), \
                 mock.patch.object(input_backend, "LEGACY_INPUT_MAP_TARGETS", (legacy_map_a, legacy_map_b)), \
                 mock.patch.object(companion_common, "supported_device", return_value=True):
                self.assertTrue(input_backend.button_map_owned(target))
                input_backend.install_button_fix()
                self.assertEqual(target.read_bytes(), current)
                target.write_bytes(b"name: External aya7 override\n")
                with self.assertRaises(RuntimeError):
                    input_backend.install_button_fix()
                self.assertEqual(target.read_bytes(), b"name: External aya7 override\n")

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
            "tdp": dict(power_backend.DEFAULT_TDP),
            "tdp_preset": "Balanced",
            "game_profiles": {"200": profile_b},
        }
        main.Plugin._active_app = "100"
        main.Plugin._state = {"tdp": dict(power_backend.DEFAULT_TDP), "tdp_preset": "Balanced"}

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
            with mock.patch.object(companion_common, "supported_device", return_value=True), \
                 mock.patch.object(power_backend, "apply_tdp", side_effect=apply), \
                 mock.patch.object(power_backend, "read_cpu_boost", return_value=True), \
                 mock.patch.object(power_backend, "write_cpu_boost"), \
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

    def test_controller_monitor_reads_state_after_pending_rpc_commits(self):
        target = {
            **controller_backend.DEFAULT_CONTROLLER,
            "vibration": "off",
            "rgb_mode": "pulse",
            "color": "ff0000",
            "ff_gain": 60,
        }
        started = threading.Event()
        release = threading.Event()
        reconciled = []
        previous_settings = dict(main.settings.data)
        previous_state = main.Plugin._state
        main.settings.data = {"controller": dict(controller_backend.DEFAULT_CONTROLLER)}
        main.Plugin._state = {"controller": dict(controller_backend.DEFAULT_CONTROLLER)}

        def apply(_value):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test synchronization timed out")

        def reconcile(value, _recover, _force, repair=True):
            reconciled.append(dict(value))
            return "healthy", "/dev/hidraw7"

        async def scenario():
            first = asyncio.create_task(main.Plugin().set_controller(target))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(asyncio.to_thread(
                main.Plugin._reconcile_current_controller, False, False))
            await asyncio.sleep(0.05)
            self.assertFalse(second.done())
            release.set()
            await asyncio.gather(first, second)

        try:
            with mock.patch.object(controller_backend, "apply_controller", side_effect=apply), \
                 mock.patch.object(controller_backend, "set_vibration_gain"), \
                 mock.patch.object(controller_backend, "reconcile_controller", side_effect=reconcile), \
                 mock.patch.object(main.Plugin, "get_state",
                                   new=mock.AsyncMock(return_value={})):
                asyncio.run(scenario())
            self.assertEqual(reconciled, [target])
            self.assertEqual(main.Plugin._state["controller"], target)
            self.assertEqual(main.settings.data["controller"], target)
        finally:
            release.set()
            main.settings.data = previous_settings
            main.Plugin._state = previous_state

    def test_updater_accepts_only_the_exact_release_asset(self):
        companion_common.updater.reset()
        class Response(io.BytesIO):
            def __init__(self, payload):
                super().__init__(json.dumps(payload).encode())
            def geturl(self): return companion_common.GITHUB_RELEASES_URL

        release = {
            "tag_name": "v1.3.0",
            "draft": False, "prerelease": False,
            "html_url": "https://github.com/Rayekkk/Ayaneo3Companion/releases/tag/v1.3.0",
            "assets": [{
                "name": "Ayaneo3Companion-1.3.0.zip",
                "state": "uploaded", "size": 1234, "digest": "sha256:" + "1" * 64,
                "browser_download_url": (
                    "https://github.com/Rayekkk/Ayaneo3Companion/releases/download/"
                    "v1.3.0/Ayaneo3Companion-1.3.0.zip"),
            }],
        }
        with mock.patch.object(companion_common.updater, "open_url", return_value=Response(release)):
            info = companion_common.updater.check()
        self.assertEqual(info["current_version"], "1.2.0")
        self.assertEqual(info["latest_version"], "1.3.0")
        self.assertTrue(info["update_available"])
        self.assertEqual(info["asset_name"], "Ayaneo3Companion-1.3.0.zip")

        release["assets"][0]["name"] = "source.zip"
        companion_common.updater._cached = None
        with mock.patch.object(companion_common.updater, "open_url", return_value=Response(release)):
            info = companion_common.updater.check()
        self.assertIn("Ayaneo3Companion-1.3.0.zip", info["error"])

    def test_release_version_and_package_inventory_agree(self):
        root = companion_common.PLUGIN_DIR
        manifest = json.loads((root / "plugin.json").read_text())
        package = json.loads((root / "package.json").read_text())
        lock = json.loads((root / "package-lock.json").read_text())
        self.assertEqual(
            {manifest["version"], package["version"], lock["version"],
             lock["packages"][""]["version"]},
            {"1.2.0"},
        )
        package_script = (root / "scripts" / "package.mjs").read_text()
        self.assertIn('"lego_updater.py"', package_script)
        self.assertIn('"fan_backend.py"', package_script)
        self.assertNotIn('"react-icons"', package_script)
        self.assertIn("THIRD_PARTY_LICENSES", package_script)
        self.assertIn("THIRD_PARTY_SOURCES", package_script)
        self.assertIn("Decky Loader API 1.1.3", (root / "NOTICE").read_text())

if __name__ == "__main__": unittest.main()
