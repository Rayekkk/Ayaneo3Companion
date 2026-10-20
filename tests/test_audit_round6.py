# SPDX-License-Identifier: BSD-3-Clause
"""Controller confirmation, bounded transfers and profile retry ownership."""
import asyncio
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_logic
from _harness import owner
import main
import audio_backend
import power_backend
import controller_backend
import ec_backend
import companion_common
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


class SlowResponse:
    def __init__(self, clock, chunks, seconds_per_chunk=0, length=None):
        self.clock, self.chunks, self.seconds = clock, list(chunks), seconds_per_chunk
        self.headers = {"Content-Length": str(length)} if length is not None else {}
        self.closed = False
        self.bulk_reads = 0
        self.incremental_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def geturl(self):
        return power_backend.RYZENADJ_URL

    def next(self):
        self.clock[0] += self.seconds
        return self.chunks.pop(0) if self.chunks else b""

    def read(self, _size):
        self.bulk_reads += 1
        return self.next()

    def read1(self, _size):
        self.incremental_reads += 1
        return self.next()


class HardwareTransferTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.clock = [0.0]
        self.stack.enter_context(patch.object(main.time, "monotonic", side_effect=lambda: self.clock[0]))

    def response(self, **kwargs):
        response = SlowResponse(self.clock, **kwargs)
        self.stack.enter_context(patch.object(companion_common, "open_hardware_download", return_value=response))
        return response

    def test_archive_transfer_has_an_overall_deadline_and_closes_stream(self):
        response = self.response(chunks=[b"archive"], seconds_per_chunk=120)
        with self.assertRaises(TimeoutError):
            power_backend._download_archive(self.root / "download.tar.gz")
        self.assertTrue(response.closed)

    def test_audio_transfer_has_an_overall_deadline_even_with_matching_digest(self):
        data = b"calibration"
        response = self.response(chunks=[data], seconds_per_chunk=120)
        with patch.object(audio_backend, "AUDIO_CALIBRATION_SHA256", hashlib.sha256(data).hexdigest()):
            with self.assertRaises(TimeoutError):
                audio_backend._download_audio_calibration()
        self.assertTrue(response.closed)

    def test_archive_uses_incremental_reads_and_preserves_complete_payload(self):
        response = self.response(chunks=[b"first", b"second"], seconds_per_chunk=0.01)
        target = self.root / "download.tar.gz"
        power_backend._download_archive(target)
        self.assertEqual(target.read_bytes(), b"firstsecond")
        self.assertEqual(response.bulk_reads, 0)
        self.assertGreater(response.incremental_reads, 0)
        self.assertTrue(response.closed)

    def test_download_size_limits_still_apply_without_content_length(self):
        self.response(chunks=[b"too-large"])
        with patch.object(power_backend, "MAX_DOWNLOAD_BYTES", 4), self.assertRaises(RuntimeError):
            power_backend._download_archive(self.root / "download.tar.gz")

    def test_timed_out_binary_install_preserves_existing_components(self):
        executable, library = self.root / "ryzenadj", self.root / "libryzenadj.so"
        executable.write_bytes(b"previous executable")
        library.write_bytes(b"previous library")
        self.response(chunks=[b"partial archive"], seconds_per_chunk=120)
        with patch.object(power_backend, "BIN_DIR", self.root), \
             patch.object(power_backend, "RYZENADJ", executable), \
             patch.object(power_backend, "RYZENADJ_LIB", library):
            with self.assertRaises(TimeoutError):
                power_backend._ensure_ryzenadj()
        self.assertEqual(executable.read_bytes(), b"previous executable")
        self.assertEqual(library.read_bytes(), b"previous library")
        self.assertEqual(set(path.name for path in self.root.iterdir()), {"ryzenadj", "libryzenadj.so"})


class DeferredProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.wanted = dict(power_backend.PRESETS["Max"])
        self.store.replace({"tdp": power_backend.DEFAULT_TDP,
                            "game_profiles": {"222": {**self.wanted, "preset": "Max"}}})
        self.state = {"supported": True, "controller": dict(controller_backend.DEFAULT_CONTROLLER),
                      "tdp": dict(self.wanted), "tdp_preset": "Max", "tdp_profile_pending": True,
                      "_applied_tdp": dict(power_backend.DEFAULT_TDP)}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main.Plugin, "_active_app", "222"))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(power_backend, "read_cpu_boost", return_value=False))
        self.stack.enter_context(patch.object(power_backend, "write_cpu_boost"))
        self.plugin = main.Plugin()

    async def test_same_game_heartbeats_do_not_duplicate_the_monitor_retry(self):
        with patch.object(main.Plugin, "_apply_tracked_tdp", side_effect=power_backend.TdpUnavailableError("PowerStation")) as apply:
            for _ in range(10):
                await self.plugin.set_active_app("222")
        apply.assert_not_called()
        self.assertTrue(self.state["tdp_profile_pending"])
        self.assertEqual(self.state["tdp"], self.wanted)

    async def test_dedicated_monitor_applies_pending_profile_after_ownership_release(self):
        asyncio.get_running_loop().set_debug(False)
        clock = 0
        async def sleep(_):
            nonlocal clock
            clock += 1
            if clock > 6:
                raise asyncio.CancelledError
        with patch.object(main.asyncio, "sleep", side_effect=sleep), \
             patch.object(main.time, "monotonic", side_effect=lambda: clock), \
             patch.object(ec_backend, "ac_online", return_value=False), \
             patch.object(main, "_suspend_clock_offset", return_value=None), \
             patch.object(main.Plugin, "_restore_saved_control"), \
             patch.object(power_backend, "apply_tdp") as apply:
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin._ac_loop()
        apply.assert_called_once_with(self.wanted)
        self.assertFalse(self.state["tdp_profile_pending"])
        self.assertEqual(self.state["_applied_tdp"], self.wanted)

    async def test_new_game_still_updates_target_and_attempts_its_profile(self):
        with patch.object(main.Plugin, "_apply_tracked_tdp", side_effect=power_backend.TdpUnavailableError("PowerStation")) as apply:
            await self.plugin.set_active_app("333")
        apply.assert_called_once_with(power_backend.DEFAULT_TDP)
        self.assertEqual(main.Plugin._active_app, "333")
        self.assertTrue(self.state["tdp_profile_pending"])
