# SPDX-License-Identifier: BSD-3-Clause
"""LC1/RC1 firmware slots, the button table and the InputPlumber map."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import support  # noqa: F401  (the Decky harness before any backend import)
import input_backend
import controller_backend
import companion_common


class KeyBindingTests(unittest.TestCase):
    def test_key_binding_map_preserves_native_buttons_and_adds_l5_r5(self):
        mapping = input_backend.button_map_bytes().decode()
        # Checked in Steam's controller tester on the console: LC/RC and LC1/RC1
        # were swapped with LC/RC on LeftTop/RightTop.
        for key, button in (("KeyF21", "LeftPaddle2"), ("KeyF22", "RightPaddle2"),
                            ("KeyL", "LeftTop"), ("KeyR", "RightTop")):
            self.assertIn(f"keyboard: {key}\n    target_event:\n      gamepad:\n        button: {button}\n", mapping)
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
                         (0x00, 0x00, 0x73), "QAM sends F24, which InputPlumber's aya7 map opens")

    def test_rear_button_programming_is_saved(self):
        calls = []

        def exchange(_fd, command, timeout=0.4, attempts=controller_backend.HID_COMMAND_ATTEMPTS):
            calls.append((command, timeout, attempts))
            return bytes(64)

        with mock.patch.object(companion_common, "supported_device", return_value=True), \
             mock.patch.object(controller_backend, "vendor_hidraw", return_value="/dev/fake"), \
             mock.patch.object(os, "open", return_value=7), \
             mock.patch.object(os, "close"), \
             mock.patch.object(os, "O_NONBLOCK", 0, create=True), \
             mock.patch.object(controller_backend, "_hid_exchange", side_effect=exchange):
            controller_backend.program_rear_buttons(True)

        table = calls[1:34]
        self.assertEqual([command[5] for command, _, _ in table], list(range(33)))
        self.assertEqual([table[0x12][0][12], table[0x13][0][12]], [0x0f, 0x15])
        self.assertEqual(calls[-3][0][20], 0x88)
        self.assertEqual(calls[-3][2], 1, "the physical module reset is never sent twice")
        self.assertEqual(calls[-2][0][20], 0x00)
        self.assertEqual(calls[-1], (controller_backend.AYA_SAVE, 4.5, 1), "one NVRAM save, not three")

    def test_input_map_upgrades_owned_legacy_file_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            target = root / "ayaneo_type7.yaml"
            current = input_backend.INPUT_MAP_SOURCE.read_bytes()
            # The unmarked map shipped before LC/RC and LC1/RC1 changed places.
            legacy = current.replace(input_backend.INPUT_MAP_MARKER, b"", 1)
            for key, old in ((b"KeyF21", b"LeftTop"), (b"KeyF22", b"RightTop"),
                             (b"KeyL", b"LeftPaddle2"), (b"KeyR", b"RightPaddle2")):
                head = b"keyboard: " + key + b"\n    target_event:\n      gamepad:\n        button: "
                start = legacy.index(head) + len(head)
                legacy = legacy[:start] + old + legacy[legacy.index(b"\n", start):]
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


if __name__ == "__main__":
    unittest.main()
