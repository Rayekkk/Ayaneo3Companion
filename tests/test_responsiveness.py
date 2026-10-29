# SPDX-License-Identifier: BSD-3-Clause
"""RPC responsiveness checks; hardware and services are isolated doubles."""
import asyncio
from contextlib import ExitStack
import tempfile
import threading
import unittest
from unittest.mock import patch
import time
import support  # noqa: F401  (the Decky harness before any backend import)
from support import owner
import main
import display_backend
import controller_backend
import ec_backend
import companion_common
from safe_settings import SettingsManager
import safe_settings


def state_of(test, response):
    """The state a successful D2 answer carries."""
    test.assertIs(response["ok"], True, response)
    return response["state"]


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.store = SettingsManager("settings", directory)
        self.controller = dict(controller_backend.DEFAULT_CONTROLLER)
        self.store.replace({"controller": self.controller})
        self.state = {"supported": True, "controller": dict(self.controller),
                      "modules_connected": True, "modules_reconnecting": False,
                      "charge_bypass_supported": True}
        self.stack.enter_context(patch.object(main, "settings", self.store))
        self.stack.enter_context(patch.object(main.Plugin, "_state", self.state))
        self.stack.enter_context(patch.object(companion_common, "supported_device", return_value=True))
        self.stack.enter_context(patch.object(controller_backend, "apply_controller"))
        self.stack.enter_context(patch.object(controller_backend, "set_vibration_gain"))
        for name, value in (("vendor_hidraw", "/fake/hidraw"),
                            ("is_our_display_script", False), ("display_script_conflict", False),
                            ("button_fix_installed", False), ("read_charge_bypass", False),
                            ("module_presence", {"left": True, "right": True})):
            self.stack.enter_context(patch.object(owner(name), name, return_value=value))
        self.stack.enter_context(patch.object(display_backend, "read_published_edid", side_effect=OSError()))
        self.plugin = main.Plugin()

    async def test_controller_ack_does_not_probe_unrelated_services(self):
        wanted = {**self.controller, "color": "00ff00"}
        with patch.object(ec_backend, "read_charge_bypass", side_effect=AssertionError("unrelated EC read")),              patch.object(ec_backend, "module_presence", side_effect=AssertionError("unrelated EC read")):
            response = state_of(self, await self.plugin.set_controller(wanted))
        self.assertEqual(response["controller"], wanted)
        self.assertEqual(self.store.getSetting("controller"), wanted)
        response["controller"]["color"] = "ffffff"
        self.assertEqual(self.state["controller"]["color"], "00ff00")

    async def test_slow_read_does_not_hold_up_write_ack_or_next_controller_edit(self):
        entered, release = threading.Event(), threading.Event()
        def probe():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test failed to release the diagnostic")
            return False
        tasks = []
        with patch.object(ec_backend, "read_charge_bypass", side_effect=probe):
            try:
                tasks.append(asyncio.create_task(self.plugin.get_state()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                tasks.append(asyncio.create_task(self.plugin.set_controller(
                    {**self.controller, "brightness": 40})))
                tasks.append(asyncio.create_task(self.plugin.set_vibration_gain(20)))
                done, _ = await asyncio.wait(tasks[1:], timeout=0.25)
                acknowledged_before_read = len(done)
            finally:
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertEqual(acknowledged_before_read, 2,
                         "both writes must finish while the unrelated read is still blocked")
        last = [state_of(self, response) for response in responses][-1]
        self.assertEqual(last["controller"]["brightness"], 40)
        self.assertEqual(last["controller"]["ff_gain"], 20)

    async def test_fast_ack_preserves_last_observed_capabilities(self):
        with patch.object(ec_backend, "read_charge_bypass", side_effect=OSError("EC busy")):
            observed = state_of(self, await self.plugin.get_state())
        self.assertFalse(observed["charge_bypass_supported"])
        with patch.object(ec_backend, "read_charge_bypass", side_effect=AssertionError("redundant probe")):
            response = state_of(self, await self.plugin.set_vibration_gain(20))
        self.assertFalse(response["charge_bypass_supported"])
        self.assertEqual(response["controller"]["ff_gain"], 20)

    async def test_diagnostic_publication_cannot_replace_new_controller_state(self):
        wanted = {**self.controller, "brightness": 30}
        def transition():
            with main._lock:
                self.state.update(controller=wanted)
            return {"left": True, "right": True}
        with patch.object(ec_backend, "module_presence", side_effect=transition):
            response = await self.plugin._offload(self.plugin._snapshot)
        self.assertEqual(response["controller"], self.controller)
        self.assertEqual(self.state["controller"], wanted)

    async def test_slow_diagnostic_does_not_replace_newer_capability_information(self):
        self.state.update(charge_bypass_supported=False)
        def recovered():
            with main._lock:
                self.state.update(charge_bypass_supported=True)
            raise OSError("old failure")
        with patch.object(ec_backend, "read_charge_bypass", side_effect=recovered):
            await self.plugin._offload(self.plugin._snapshot)
        self.assertTrue(self.state["charge_bypass_supported"])


class SettingsResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    setUp = ResponseTests.setUp
    asyncTearDown = ResponseTests.asyncTearDown

    async def assert_loop_responsive(self, action, finishes_during_commit):
        entered, release = threading.Event(), threading.Event()
        write = safe_settings.dk_fs.write_json
        def stalled_write(path, payload, *args, **kwargs):
            if path == self.store.path:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release the settings write")
            return write(path, payload, *args, **kwargs)

        tasks = []
        with patch.object(safe_settings.dk_fs, "write_json", side_effect=stalled_write):
            tasks.append(asyncio.create_task(self.plugin._offload(self.store.commit)))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # The old implementation blocks the loop itself. A thread
                # releases the test disk even if asyncio cannot run cleanup.
                watchdog = threading.Timer(0.65, release.set)
                watchdog.start()
                started = time.monotonic()
                operation = asyncio.create_task(action())
                tasks.append(operation)
                async def heartbeat():
                    await asyncio.sleep(0.02)
                    return time.monotonic() - started
                beat = asyncio.create_task(heartbeat())
                tasks.append(beat)
                elapsed = await beat
                done, _ = await asyncio.wait([operation], timeout=0.1)
                finished = operation in done
            finally:
                release.set()
                if "watchdog" in locals():
                    watchdog.cancel()
                    watchdog.join()
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
        self.assertLess(elapsed, 0.3, "settings fsync must not block the event loop heartbeat")
        if finishes_during_commit:
            self.assertTrue(finished, "this independent operation must finish before settings disk I/O")
        return results[1]

    async def test_fan_off_remains_responsive_during_settings_disk_io(self):
        # Fan Control keeps its own store and lock: turning it off never waits for main's settings write.
        off = dict(success=True, settings=dict(enabled=False))
        with patch.object(self.plugin._fan, "set_enabled", return_value=off) as set_enabled:
            result = await self.assert_loop_responsive(lambda: self.plugin.fan_set_enabled(False), True)
        self.assertEqual(result, {"ok": True, "state": {"settings": {"enabled": False}}})
        set_enabled.assert_awaited_once_with(False)
