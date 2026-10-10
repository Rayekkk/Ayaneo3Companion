# SPDX-License-Identifier: BSD-3-Clause
"""Use the real selector and FF_GAIN writer, redirecting evdev to temp files."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

import test_logic
import main


INTERNAL, EXTERNAL = "/dev/input/event5", "/dev/input/event9"


class PhysicalRumbleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.paths = {node: root / Path(node).name for node in (INTERNAL, EXTERNAL)}
        for path in self.paths.values():
            path.write_bytes(b"")
        self.nodes = [EXTERNAL]
        self.opened = []
        self.identities = {INTERNAL: ("045e", "028e", sorted(main.AYA3_GAMEPAD_PHYS_PATHS)[0]),
                           EXTERNAL: ("045e", "028e", "usb-0000:c7:00.0-4/input0")}
        self.state = {"supported": True, "controller": dict(main.DEFAULT_CONTROLLER),
                      "modules_connected": True, "modules_reconnecting": False}
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(main, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(main.glob, "glob", side_effect=lambda _: list(self.nodes)))
        self.stack.enter_context(patch.object(main, "_event_has_rumble", return_value=True))
        self.stack.enter_context(patch.object(main, "_input_event_identity",
                                            side_effect=lambda node: self.identities[node]))
        self.stack.enter_context(patch.object(main, "_suspend_clock_offset", return_value=None))
        real_open, real_stat = os.open, os.stat
        def open_node(node, flags, *args, **kwargs):
            # Unexpected device I/O is an error, never forwarded to the host.
            if str(node).startswith("/dev/"):
                if node not in self.paths:
                    raise AssertionError(f"unexpected device access: {node}")
                self.opened.append(node)
                # Windows' default text mode would expand a timestamp's 0x0a.
                return real_open(self.paths[node], flags | getattr(os, "O_BINARY", 0), *args, **kwargs)
            return real_open(node, flags, *args, **kwargs)
        def stat_node(node, *args, **kwargs):
            if node in self.paths:
                return types.SimpleNamespace(st_rdev=1, st_ino=5 if node == INTERNAL else 9)
            return real_stat(node, *args, **kwargs)
        self.stack.enter_context(patch.object(main.os, "open", side_effect=open_node))
        self.stack.enter_context(patch.object(main.os, "stat", side_effect=stat_node))

    async def test_single_external_xbox_and_ayaneo_vid_never_become_internal(self):
        for vendor, product in (("045e", "028e"), ("1c4f", "0001")):
            with self.subTest(vendor=vendor):
                self.identities[EXTERNAL] = (vendor, product, "usb-external/input0")
                self.assertIsNone(main._rumble_event_node())
                self.assertIsNone(main._rumble_event_token())
        self.assertFalse(self.opened)

    async def test_manual_gain_and_test_do_not_open_external_pad(self):
        for percent in (0, 60, 100):
            with self.subTest(percent=percent), self.assertRaises(RuntimeError):
                main.set_vibration_gain(percent)
        with patch.dict("sys.modules", {"fcntl": types.SimpleNamespace(ioctl=lambda *args: None)}):
            with self.assertRaises(RuntimeError):
                main.play_vibration_test("high", 100)
        self.assertFalse(self.opened)
        self.assertEqual(self.paths[EXTERNAL].read_bytes(), b"")

    async def test_verified_internal_topologies_still_receive_gain(self):
        self.nodes = [EXTERNAL, INTERNAL]
        for phys in main.AYA3_GAMEPAD_PHYS_PATHS:
            for vendor, product in (("045e", "028e"), ("1c4f", "0001")):
                with self.subTest(phys=phys, vendor=vendor):
                    self.identities[INTERNAL] = (vendor, product, phys)
                    self.assertEqual(main._rumble_event_node(), INTERNAL)
                    main.set_vibration_gain(60)
        self.assertEqual(set(self.opened), {INTERNAL})
        self.assertEqual(self.paths[EXTERNAL].read_bytes(), b"")
        self.assertEqual(struct.unpack("<qqHHi", self.paths[INTERNAL].read_bytes())[2:],
                         (main.EV_FF, main.FF_GAIN, round(0xffff * 0.6)))

    async def test_monitor_waits_for_internal_pad_and_restores_only_it_after_hotplug(self):
        for blocked in (False, True):
            for gain in (0, 60, 100):
                with self.subTest(blocked=blocked, gain=gain):
                    self.opened.clear()
                    for path in self.paths.values():
                        path.write_bytes(b"")
                    self.state["controller"]["ff_gain"] = gain
                    phase = 0
                    async def sleep(_):
                        nonlocal phase
                        phase += 1
                        if phase > 4:
                            raise asyncio.CancelledError
                        self.nodes = [EXTERNAL] if phase in (1, 3) else [EXTERNAL, INTERNAL]
                    with patch.object(main.asyncio, "sleep", side_effect=sleep), \
                         patch.object(main, "_vendor_hidraw", side_effect=RuntimeError("hid-ayaneo")
                                      if blocked else None, return_value="/fake/hidraw"), \
                         patch.object(main, "_hid_exchange", side_effect=AssertionError("HID forbidden")):
                        with self.assertRaises(asyncio.CancelledError):
                            await main.Plugin()._vibration_gain_loop()
                    self.assertEqual(self.opened, [INTERNAL, INTERNAL])
                    self.assertEqual(self.paths[EXTERNAL].read_bytes(), b"")
                    self.assertEqual(struct.unpack("<qqHHi", self.paths[INTERNAL].read_bytes())[2:],
                                     (main.EV_FF, main.FF_GAIN, round(0xffff * gain / 100)))
