# SPDX-License-Identifier: BSD-3-Clause
"""AYANEO controller packets, HID transactions, device discovery and FF_GAIN."""
import subprocess
import glob
import select
import asyncio
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
from support import controller_report
import main
import input_backend
import display_backend
import audio_backend
import tdp_backend
import controller_backend
import ec_backend
import companion_common


class ControllerProtocolTests(unittest.TestCase):
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

    def test_vendor_hidraw_skips_an_external_device_with_the_same_chip(self):
        with tempfile.TemporaryDirectory() as directory:
            nodes = []
            for name, phys in (("hidraw3", "usb-0000:c4:00.4-2/input1"), ("hidraw5", "usb-0000:c4:00.3-1/input2")):
                device = Path(directory) / name / "device"
                device.mkdir(parents=True)
                (device / "report_descriptor").write_bytes(b"\x06\x00\xff\x09\x01")
                (device / "uevent").write_text(f"HID_ID=0003:00001C4F:00000002\nHID_PHYS={phys}\n")
                nodes.append(str(device.parent))
            with mock.patch.object(glob, "glob", return_value=nodes), \
                 mock.patch.object(controller_backend, "_vendor_hidraw_cached", None):
                self.assertEqual(controller_backend.vendor_hidraw(), "/dev/hidraw5")

    def test_vendor_hidraw_rejects_active_kernel_driver(self):
        with tempfile.TemporaryDirectory() as directory:
            hidraw = Path(directory) / "hidraw7"
            device = hidraw / "device"
            device.mkdir(parents=True)
            (device / "report_descriptor").write_bytes(b"\x06\x00\xff\x09\x01")
            (device / "uevent").write_text("HID_ID=0003:00001C4F:00000002\nHID_PHYS=usb-0000:c4:00.3-1/input2\n")
            for name in ("module_left", "module_right", "eject", "reset"):
                (device / name).touch()
            with mock.patch.object(glob, "glob", return_value=[str(hidraw)]):
                with self.assertRaisesRegex(RuntimeError, "kernel hid-ayaneo is active"):
                    controller_backend.vendor_hidraw()

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

    def test_unsupported_device_blocks_privileged_hardware_writes(self):
        controller = dict(controller_backend.DEFAULT_CONTROLLER)
        operations = (
            (ec_backend._write_ec_register, (0x10, 0x01)),
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
            # TDP Control reports a refusal instead of raising; it never writes either.
            with mock.patch.object(tdp_backend, "settings", tdp_backend.SettingsManager(
                    name="tdp_settings", settings_directory=tempfile.mkdtemp())):
                self.assertFalse(tdp_backend._apply_limits(15000, 25000, 30000)["success"])
                self.assertFalse(tdp_backend._apply_cpu_boost_hardware(False)["success"])
        run.assert_not_called()

    def test_unsupported_state_snapshot_skips_ayaneo_hardware_probes(self):
        previous_state = main.Plugin._state
        main.Plugin._state = {
            "supported": False,
            "controller": dict(controller_backend.DEFAULT_CONTROLLER),
            "module_left": controller_backend.module_info("left"),
            "module_right": controller_backend.module_info("right"),
            "charge_bypass_supported": False,
            "module_eject_supported": False,
            "module_reset_supported": False,
        }
        try:
            with mock.patch.object(controller_backend, "vendor_hidraw") as hidraw, \
                 mock.patch.object(ec_backend, "read_charge_bypass") as charge, \
                 mock.patch.object(ec_backend, "module_presence") as modules:
                snapshot = main.Plugin._snapshot()
            self.assertFalse(snapshot["supported"])
            hidraw.assert_not_called()
            charge.assert_not_called()
            modules.assert_not_called()
        finally:
            main.Plugin._state = previous_state

    def test_dmi_is_read_once_but_a_failed_read_is_retried(self):
        answers = iter([OSError("not yet"), "AYANEO 3\n"])
        reads = []
        def read(path):
            reads.append(path)
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.dict(companion_common._dmi_cache, clear=True), \
             mock.patch.object(Path, "read_text", read):
            self.assertEqual(companion_common.dmi("product_name"), "")
            self.assertEqual(companion_common.dmi("product_name"), "AYANEO 3")
            self.assertEqual(companion_common.dmi("product_name"), "AYANEO 3")
            self.assertEqual(len(reads), 2)

    def test_vendor_hidraw_is_scanned_again_only_when_its_node_changes(self):
        import types
        inode = [10]
        stat = lambda path: types.SimpleNamespace(st_ino=inode[0], st_rdev=7)
        with mock.patch.object(controller_backend, "_vendor_hidraw_cached", None), \
             mock.patch.object(controller_backend.glob, "glob", return_value=["/sys/class/hidraw/hidraw2"]) as found, \
             mock.patch.object(Path, "resolve", lambda path: path), \
             mock.patch.object(Path, "read_bytes", lambda path: b"\x06\x00\xff\x09\x01"), \
             mock.patch.object(Path, "read_text", lambda path: "HID_ID=0003:00001C4F:00000002\nHID_PHYS=usb-0000:c4:00.3-1/input2\n"), \
             mock.patch.object(controller_backend, "_kernel_hid_ayaneo_active", return_value=False), \
             mock.patch.object(controller_backend.os, "stat", side_effect=stat):
            self.assertEqual(controller_backend.vendor_hidraw(), "/dev/hidraw2")
            self.assertEqual(controller_backend.vendor_hidraw(), "/dev/hidraw2")
            self.assertEqual(found.call_count, 1, "the same node is reused without a scan")
            inode[0] = 11  # TM or a module swap re-enumerated the controller
            self.assertEqual(controller_backend.vendor_hidraw(), "/dev/hidraw2")
            self.assertEqual(found.call_count, 2)

    def test_input_node_identity_is_read_again_only_for_a_recreated_node(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            node = root / "event5"; node.write_text("")
            ident = root / "sys" / "event5" / "device"; (ident / "id").mkdir(parents=True)
            for leaf, value in (("id/vendor", "1c4f"), ("id/product", "0002"), ("phys", "usb-1/input1")):
                (ident / leaf).write_text(value + "\n")
            reads = []
            real = Path.read_text
            def counting(path, *args, **kwargs):
                reads.append(path.name)
                return real(path, *args, **kwargs)
            with mock.patch.object(controller_backend, "INPUT_SYSFS", root / "sys"), \
                 mock.patch.dict(controller_backend._event_identity_cache, clear=True), \
                 mock.patch.object(Path, "read_text", counting):
                first = controller_backend._input_event_identity(str(node))
                self.assertEqual(first, ("1c4f", "0002", "usb-1/input1"))
                self.assertEqual(controller_backend._input_event_identity(str(node)), first)
                self.assertEqual(len(reads), 3, "the second call reads nothing from sysfs")
                (ident / "id" / "vendor").write_text("045e\n")
                # The kernel recreated the node. Made before the old one goes, so ext4 cannot reuse its inode.
                fresh = root / "event5.new"; fresh.write_text("x"); os.replace(fresh, node)
                second = controller_backend._input_event_identity(str(node))
                self.assertEqual(second[0], "045e")
                (ident / "id" / "vendor").write_text("\n")
                gone = root / "event6"  # a node that is being created: never cached empty
                gone.write_text("")
                with mock.patch.object(controller_backend, "INPUT_SYSFS", root / "missing"):
                    self.assertEqual(controller_backend._input_event_identity(str(gone)), ("", "", ""))
                self.assertNotIn(str(gone), controller_backend._event_identity_cache)

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

    def test_unload_does_not_probe_or_write_controller_after_device_mismatch(self):
        previous_state = main.Plugin._state
        task_names = (
            "_audio_task", "_restore_task", "_edid_task", "_charge_task",
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


if __name__ == "__main__":
    unittest.main()
