# SPDX-License-Identifier: BSD-3-Clause
"""Controller confirmation, bounded transfers and profile retry ownership."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import support  # noqa: F401  (the Decky harness before any backend import)
from _harness import owner
import main
import controller_backend
from safe_settings import SettingsManager


def controller_report(config):
    packet = controller_backend.controller_command(config)
    response = bytearray(64)
    response[3] = controller_backend.AYA_CHECK[4]
    response[7:15] = packet[8:16]
    response[21:24] = packet[22:25]
    return bytes(response)


class ControllerConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("supported_device", True), ("vendor_hidraw", "/fake/hidraw")):
            self.stack.enter_context(patch.object(owner(name), name, return_value=value))
        self.stack.enter_context(patch.object(os, "O_NONBLOCK", 0, create=True))
        real_open, real_close = os.open, os.close
        self.stack.enter_context(patch.object(os, "open", side_effect=lambda path, *args, **kwargs:
            999997 if path == "/fake/hidraw" else real_open(path, *args, **kwargs)))
        self.closed = self.stack.enter_context(patch.object(os, "close", side_effect=lambda fd:
            None if fd == 999997 else real_close(fd)))
        self.gain = self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        self.wanted = {**controller_backend.DEFAULT_CONTROLLER, "brightness": 25}
        self.previous = dict(controller_backend.DEFAULT_CONTROLLER)

    def exchange(self, readbacks):
        replies = iter(readbacks)
        commands = []
        def send(_fd, command, **_kwargs):
            commands.append(command)
            if command == controller_backend.AYA_CHECK:
                return next(replies)
            ack = bytearray(64)
            ack[3] = command[4]
            return bytes(ack)
        self.stack.enter_context(patch.object(controller_backend, "_hid_exchange", side_effect=send))
        return commands

    def test_missing_or_malformed_readback_cannot_confirm_a_write(self):
        for bad in (b"", b"short", bytes(64)):
            with self.subTest(reply=bad), ExitStack() as case:
                commands = []
                replies = iter((controller_report(self.previous), bad, bad))
                def send(_fd, command, **_):
                    commands.append(command)
                    return next(replies) if command == controller_backend.AYA_CHECK else bytes(64)
                case.enter_context(patch.object(controller_backend, "_hid_exchange", side_effect=send))
                feedback = case.enter_context(patch.object(controller_backend, "play_vibration_test"))
                with self.assertRaises(RuntimeError):
                    controller_backend.apply_controller(self.wanted, vibration_feedback=True)
                self.assertNotIn(controller_backend.AYA_SAVE, commands)
                feedback.assert_not_called()
                self.assertLessEqual(commands.count(controller_backend.controller_command(self.wanted)), 2)

    def test_transient_missing_readback_retries_and_confirms(self):
        commands = self.exchange((controller_report(self.previous), b"", controller_report(self.wanted)))
        controller_backend.apply_controller(self.wanted, persist_firmware=False)
        self.assertEqual(commands.count(controller_backend.controller_command(self.wanted)), 2)
        self.assertNotIn(controller_backend.AYA_SAVE, commands)

    def test_matching_settings_in_tm_mode_are_not_confirmation(self):
        wrong_mode = bytearray(controller_report(self.wanted))
        wrong_mode[controller_backend.AYA_CUSTOM_REQUIRED_INDEX] = 1
        self.exchange((controller_report(self.previous), bytes(wrong_mode), bytes(wrong_mode)))
        with self.assertRaises(RuntimeError):
            controller_backend.apply_controller(self.wanted, persist_firmware=False)

    def test_monitor_cannot_report_restore_from_an_ack_without_readback(self):
        self.exchange((controller_report(self.previous), b"", b""))
        with self.assertRaises(RuntimeError):
            controller_backend.reconcile_controller(self.wanted, True)
        self.gain.assert_not_called()

    def test_healthy_monitor_keeps_one_read_and_no_write(self):
        commands = self.exchange((controller_report(self.wanted),))
        self.assertEqual(controller_backend.reconcile_controller(self.wanted, True), ("healthy", "/fake/hidraw"))
        self.assertEqual(commands, [controller_backend.AYA_CHECK])
        self.gain.assert_not_called()

    def test_unconfirmed_rpc_does_not_save_and_rolls_back_previous_configuration(self):
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        store = SettingsManager("settings", directory)
        store.replace({"controller": self.previous})
        state = {"supported": True, "controller": dict(self.previous)}
        self.stack.enter_context(patch.object(main, "settings", store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", state))
        self.exchange((controller_report(self.previous), b"", b"",
                       controller_report(self.previous), controller_report(self.previous)))
        before = Path(store.path).read_bytes()
        with self.assertRaisesRegex(RuntimeError, "previous hardware settings restored"):
            asyncio.run(main.Plugin().set_controller(self.wanted))
        self.assertEqual(Path(store.path).read_bytes(), before)
        self.assertEqual(store.getSetting("controller"), self.previous)
        self.assertEqual(state["controller"], self.previous)


