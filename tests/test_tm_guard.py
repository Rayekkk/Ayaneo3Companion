# SPDX-License-Identifier: BSD-3-Clause
"""TM Guard: TM mode detection, the power cycle out of it, latched keys and resume."""
import asyncio
import contextlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
from support import controller_report
import main
import controller_backend
import companion_common


class TmGuardTests(unittest.TestCase):
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

    def test_hx370_custom_mode_flag_uses_status_byte_18(self):
        response = bytes.fromhex(
            "0000000800000c019100ff019100ff000000010000332230000000000000000144"
            "5000006464000000000000000000000000000000000000000000000000000000")
        self.assertEqual(response[17], 0)
        self.assertEqual(response[18], 1)
        self.assertTrue(controller_backend.controller_requires_custom(response))

    def test_tm_button_mode_3_requires_custom_and_custom_mode_2_does_not(self):
        # AYA_CHECK answers captured on the console in custom mode and right after one TM press.
        custom = bytearray(64)
        for index, value in {3: 0x08, 6: 0x0C, 7: 0x01, 8: 0xD1, 10: 0x7A, 11: 0x01, 12: 0xD1, 14: 0x7A, 18: 0x02,
                             21: 0x33, 22: 0x22, 23: 0x40, 31: 0x01, 32: 0x48, 33: 0x56, 36: 0x64, 37: 0x64}.items():
            custom[index] = value
        tm = bytearray(custom); tm[18] = 0x03
        self.assertFalse(controller_backend.controller_requires_custom(bytes(custom)))
        self.assertTrue(controller_backend.controller_requires_custom(bytes(tm)))
        for unknown in (0, 4, 0xFF):  # an unknown mode is never switched blindly
            tm[18] = unknown
            self.assertFalse(controller_backend.controller_requires_custom(bytes(tm)))

    def test_leaving_tm_mode_resets_only_the_gamepad_still_in_its_tm_identity(self):
        import errno
        import types
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, ids, bus, dev in (("1-1", ("1c4f", "0002"), 1, 6), ("1-2", ("4001", "0428"), 1, 7)):
                (root / name).mkdir()
                for leaf, value in (("idVendor", ids[0]), ("idProduct", ids[1]), ("busnum", bus), ("devnum", dev)):
                    (root / name / leaf).write_text(f"{value}\n")
            opened, resets = [], []
            def ioctl(fd, request, arg):
                resets.append((fd, request))
                raise OSError(errno.ENODEV, "No such device")  # as measured: the reset re-enumerates at once
            with mock.patch.object(controller_backend, "USB_DEVICES", root), \
                 mock.patch.dict(sys.modules, fcntl=types.SimpleNamespace(ioctl=ioctl)), \
                 mock.patch.object(os, "open", side_effect=lambda path, flags: opened.append(path) or 9), \
                 mock.patch.object(os, "close") as close, \
                 mock.patch.object(controller_backend.time, "sleep") as sleep, \
                 mock.patch.object(controller_backend, "_rumble_event_node",
                                   side_effect=[None, None, "/dev/input/event7"]) as rumble:
                self.assertTrue(controller_backend._reset_stale_tm_gamepad())
            self.assertEqual(rumble.call_count, 3, "waits for the Xbox pad before FF_GAIN and LC1/RC1")
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(opened, ["/dev/bus/usb/001/007"])
            self.assertEqual(resets, [(9, controller_backend.USBDEVFS_RESET)])
            close.assert_called_once_with(9)
            # Already the Xbox pad: nothing to reset.
            (root / "1-2" / "idVendor").write_text("045e\n"); (root / "1-2" / "idProduct").write_text("028e\n")
            with mock.patch.object(controller_backend, "USB_DEVICES", root), \
                 mock.patch.object(os, "open", side_effect=AssertionError("no reset")):
                self.assertFalse(controller_backend._reset_stale_tm_gamepad())

    def test_only_an_arrow_on_the_controllers_own_keyboard_counts_as_latched(self):
        import types
        held = {"/dev/input/event4": {103}, "/dev/input/event7": {108}, "/dev/input/event9": {38}}
        identity = {"/dev/input/event4": ("1c4f", "0002", "usb-0000:c4:00.3-1/input1"),
                    "/dev/input/event7": ("045e", "028e", "usb-0000:c4:00.3-2/input0"),
                    "/dev/input/event9": ("1c4f", "0002", "usb-0000:c4:00.3-1/input1")}
        opened = {}
        def ioctl(fd, request, keys):
            self.assertEqual(request, controller_backend.EVIOCGKEY)
            for key in held[opened[fd]]:
                keys[key // 8] |= 1 << (key % 8)
        def open_node(path, flags):
            opened[len(opened) + 10] = path
            return len(opened) + 9
        for nodes, expected in ((["/dev/input/event4"], True),  # Up on the controller keyboard
                                (["/dev/input/event7"], False),  # D-pad down on the Xbox pad is real input
                                (["/dev/input/event9"], False)):  # L from LC1 is not an arrow
            opened.clear()
            with self.subTest(nodes=nodes), \
                 mock.patch.object(controller_backend.glob, "glob", return_value=nodes), \
                 mock.patch.object(controller_backend, "_input_event_identity", side_effect=identity.get), \
                 mock.patch.dict(sys.modules, fcntl=types.SimpleNamespace(ioctl=ioctl)), \
                 mock.patch.object(os, "open", side_effect=open_node), \
                 mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
                 mock.patch.object(os, "close"):
                self.assertEqual(controller_backend.latched_arrow_key(), expected)

    def test_tm_mode_is_left_only_after_a_power_cycle(self):
        config = dict(controller_backend.DEFAULT_CONTROLLER)
        tm = bytearray(controller_report(config)); tm[controller_backend.AYA_CUSTOM_REQUIRED_INDEX] = 3
        sent, clock = [], [1000.0]
        def exchange(_fd, command, timeout=0.4):
            sent.append(command)
            return bytes(tm) if command == controller_backend.AYA_CHECK else bytes(64)
        with contextlib.ExitStack() as stack:
            for item in (mock.patch.object(companion_common, "supported_device", return_value=True),
                         mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"),
                         mock.patch.object(os, "open", return_value=7), mock.patch.object(os, "close"),
                         mock.patch.object(os, "O_NONBLOCK", 0, create=True),
                         mock.patch.object(controller_backend.time, "sleep"),
                         mock.patch.object(controller_backend.time, "monotonic", side_effect=lambda: clock[0]),
                         mock.patch.object(controller_backend, "_tm_power_cycled_at", None),
                         mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange),
                         mock.patch.object(controller_backend, "_write_verified_controller"),
                         mock.patch.object(controller_backend, "set_vibration_gain")):
                stack.enter_context(item)
            power = stack.enter_context(mock.patch.object(controller_backend.ec_backend, "set_controller_power"))
            switch = stack.enter_context(mock.patch.object(controller_backend, "_switch_to_custom_mode_fd"))
            self.assertEqual(controller_backend.reconcile_controller(config, True)[0], "tm_power_cycle")
            switch.assert_not_called()
            self.assertEqual(sent, [controller_backend.AYA_CHECK], "nothing is written before the power cycle")
            controller_backend.power_cycle_controller()
            self.assertEqual(power.call_args_list, [mock.call(False), mock.call(True)])
            clock[0] += 5
            self.assertEqual(controller_backend.reconcile_controller(config, True)[0], "mode_restored")
            switch.assert_called_once()
            clock[0] += controller_backend.TM_SWITCH_WINDOW_S
            self.assertEqual(controller_backend.reconcile_controller(config, True)[0], "tm_power_cycle",
                             "a later TM press is power-cycled again")
            controller_backend.power_cycle_controller()
            controller_backend._consume_power_cycle()  # what a real switch does
            clock[0] += 1
            self.assertEqual(controller_backend.reconcile_controller(config, True)[0], "tm_power_cycle",
                             "a TM press right after a recovery still gets its own power cycle")

    def test_switch_out_of_tm_mode_resets_the_gamepad_after_the_mode_is_verified(self):
        tm, custom = bytearray(64), bytearray(64)
        tm[3] = custom[3] = controller_backend.AYA_CHECK[4]
        tm[18], custom[18] = 3, 2
        answers = iter([bytes(custom), bytes(custom)])
        with mock.patch.object(controller_backend, "_hid_exchange", side_effect=lambda fd, command: next(answers)), \
             mock.patch.object(controller_backend.time, "sleep"), \
             mock.patch.object(controller_backend, "_tm_power_cycled_at", 5.0), \
             mock.patch.object(controller_backend, "_reset_stale_tm_gamepad") as reset:
            self.assertTrue(controller_backend._switch_to_custom_mode_fd(7, bytes(tm)))
            self.assertIsNone(controller_backend._tm_power_cycled_at, "every switch uses up the power cycle")
        reset.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
