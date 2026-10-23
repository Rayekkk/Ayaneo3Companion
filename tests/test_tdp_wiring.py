# SPDX-License-Identifier: BSD-3-Clause
"""main composes TDP Control: RPC arguments pass through and 1.2.x keys move to its store once."""
import asyncio
from contextlib import ExitStack
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_logic  # noqa: F401  (installs the Decky stubs before backend imports)
import main
import tdp_backend
from safe_settings import SettingsManager


class TdpWiringTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.store = SettingsManager("settings", self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(main, "settings", self.store))

    def test_main_passes_scope_and_expected_game_without_dropping_arguments(self):
        plugin = main.Plugin()
        plugin._tdp = type("FakeTdp", (), {name: AsyncMock(return_value={}) for name in (
            "get_cpu_power_controls", "set_cpu_boost", "set_epp", "apply_tdp", "set_game_ac_profile")})()

        async def scenario():
            await plugin.get_cpu_power_controls("111", True)
            await plugin.set_cpu_boost(False, "111", True, "111")
            await plugin.set_epp("26", "111", True, "111", True)
            await plugin.apply_tdp(15000, 25000, 30000, "111", "balanced", "111")
            await plugin.set_game_ac_profile("111", 30000, 43000, 45000, True, "performance")
        asyncio.run(scenario())
        plugin._tdp.get_cpu_power_controls.assert_awaited_once_with("111", True)
        plugin._tdp.set_cpu_boost.assert_awaited_once_with(False, "111", True, "111", False)
        plugin._tdp.set_epp.assert_awaited_once_with("26", "111", True, "111", True)
        plugin._tdp.apply_tdp.assert_awaited_once_with(15000, 25000, 30000, "111", "balanced", "111", False)
        plugin._tdp.set_game_ac_profile.assert_awaited_once_with(
            "111", 30000, 43000, 45000, True, "performance", False)

    def test_legacy_tdp_keys_move_to_the_tdp_store_and_leave_the_main_one(self):
        self.store.replace({"tdp": {"spl": 8, "sppt": 10, "fppt": 12}, "tdp_preset": "Low power",
                            "cpu_boost": True, "game_profiles": {}, "controller": {"brightness": 40}})
        tdp_store = SettingsManager("tdp_settings", self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(tdp_backend, "settings", tdp_store))
        plugin = main.Plugin()
        asyncio.run(plugin._tdp._migration())
        self.assertEqual(SettingsManager("settings", self.store.directory).data, {"controller": {"brightness": 40}})
        state = tdp_backend._load_settings()
        self.assertEqual((state["spl"], state["active_preset"], state["cpu_boost_enabled"]),
                         (10000, "silent", True))

    def test_unreadable_main_settings_are_never_imported(self):
        with patch.object(self.store, "_recovery_error", "corrupt"):
            self.assertEqual(main.Plugin._legacy_tdp_settings(), {})


if __name__ == "__main__":
    unittest.main()
