# SPDX-License-Identifier: BSD-3-Clause
"""The AYANEO 3 side of TDP Control: ceilings, presets, 1.2.x settings, the RyzenAdj helper,
PowerStation ownership and the SPL clamp the SMU applies on every charger change."""
import asyncio
from contextlib import ExitStack
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import support  # noqa: F401  (the Decky harness before any backend import)
import tdp_backend as tdp
from safe_settings import AtomicSettingsManager

HELPER = b"\x7fELF fake ryzenadj"
INFO = """CPU Family: Hawk Point
| STAPM LIMIT         |    {spl:.3f} | stapm-limit        |
| PPT LIMIT FAST      |    {fppt:.3f} | fast-limit         |
| PPT LIMIT SLOW      |    {sppt:.3f} | slow-limit         |
"""
BLOCKED = ("CPU Family: Hawk Point\nUnable to get memory access\n"
           "Unable to init power metric table: -5, this does not affect adjustments\n")
POSIX = unittest.skipUnless(os.name == "posix", "POSIX ownership and modes")


def archive(member=b"ryzenadj", payload=HELPER):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as bundle:
        info = tarfile.TarInfo("ryzenadj-linux/" + member.decode())
        info.size = len(payload)
        bundle.addfile(info, io.BytesIO(payload))
    return data.getvalue()


class AyaneoTdpTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.store = AtomicSettingsManager("tdp_settings", str(self.root / "settings"))
        self.bin_dir = self.root / "helper"
        self.archive = archive()
        self.smu = [15.0, 25.0, 30.0]  # what the SMU holds, in watts
        self.readback = True
        self.writes = []
        self.ac = True
        for name, value in {
            "settings": self.store, "BIN_DIR": str(self.bin_dir),
            "BIN_PATH": str(self.bin_dir / "ryzenadj"),
            "RYZENADJ_SHA256": hashlib.sha256(self.archive).hexdigest(),
            "RYZENADJ_BINARY_SHA256": hashlib.sha256(HELPER).hexdigest(),
            "_ryzenadj_available": False, "_readback_available": False,
            "_current_game_id": "", "_current_ac_online": True, "_ac_target": (),
            "_ac_generation": 0, "_ac_changed_at": float("-inf"), "_transition_key": None,
            "_transition_failures": 0, "_transition_retry_at": 0.0,
            "_drift_target": (), "_drift_settled": (), "_drift_attempts": 0,
            "_last_cpu_power_check": 0.0, "_last_source": "",
        }.items():
            self.stack.enter_context(patch.object(tdp, name, value))
        tdp._invalidate_limits_cache()
        self.addCleanup(tdp._invalidate_limits_cache)
        self.stack.enter_context(patch.object(tdp, "_supported_model", return_value=True))
        self.stack.enter_context(patch.object(tdp, "_powerstation_active", return_value=False))
        self.stack.enter_context(patch.object(tdp, "_get_running_appid", return_value=""))
        self.stack.enter_context(patch.object(tdp, "_get_ac_online", side_effect=lambda: self.ac))
        self.stack.enter_context(patch.object(tdp, "_resume_detected", return_value=False))
        self.stack.enter_context(patch.object(tdp, "_best_effort_reapply_saved_cpu_power_locked",
                                              return_value=[]))
        self.stack.enter_context(patch.object(tdp, "_spawn_ryzenadj", side_effect=self.spawn))
        self.stack.enter_context(patch.object(tdp, "_download_archive", side_effect=self.download))
        self.downloads = 0

    def spawn(self, args, timeout):
        if args == ["--info"]:
            if not self.readback:
                return 1, BLOCKED, ""
            return 0, INFO.format(spl=self.smu[0], sppt=self.smu[1], fppt=self.smu[2]), ""
        values = dict(arg[2:].split("=") for arg in args)
        self.writes.append(tuple(int(values[key]) for key in ("stapm-limit", "slow-limit", "fast-limit")))
        self.smu = [min(int(values["stapm-limit"]) / 1000, 30.0),  # the SMU's own clamps
                    min(int(values["slow-limit"]) / 1000, 43.0), int(values["fast-limit"]) / 1000]
        return 0, "", ""

    def download(self, out, cancel=None):
        self.downloads += 1
        out.write(self.archive)

    def seed(self, **values):
        self.store.replace({"settings": {**tdp._defaults(), **values}, "game_profiles": {}})

    # Ceilings and presets ----------------------------------------------------------

    def test_presets_and_ceilings_match_the_measured_smu(self):
        self.assertEqual(tdp.PRESETS, {
            "silent": {"spl": 10, "sppt": 15, "fppt": 20},
            "balanced": {"spl": 15, "sppt": 25, "fppt": 30},
            "performance": {"spl": 30, "sppt": 43, "fppt": 45},
        })
        self.assertEqual(tdp.CEILINGS_MW, (30000, 43000, 45000))
        cases = {
            (50000, 50000, 50000): (30000, 43000, 45000),  # never above the ceilings
            (1000, 1000, 1000): (5000, 5000, 5000),       # never below 5 W
            (20000, 15000, 18000): (20000, 20000, 20000),  # SPL <= SPPT <= FPPT
            (10000, 40000, 30000): (10000, 30000, 30000),
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(tdp._clamp_triplet(*given), expected)

    def test_a_fresh_store_starts_balanced_and_names_what_runs(self):
        state = tdp._load_settings()
        self.assertEqual((state["spl"], state["sppt"], state["fppt"]), (15000, 25000, 30000))
        self.seed(spl=30000, sppt=43000, fppt=45000, active_preset="performance",
                  active_spl=30000, active_sppt=43000, active_fppt=45000)
        self.assertEqual(tdp._running(tdp._load_settings()),
                         {"spl": 30, "sppt": 43, "fppt": 45, "preset": "performance", "game": False})

    # 1.2.x settings ----------------------------------------------------------------

    def test_legacy_presets_take_their_new_values_and_the_rest_stays_custom(self):
        legacy = {"tdp": {"spl": 30, "sppt": 32, "fppt": 35}, "tdp_preset": "Performance",
                  "cpu_boost": False,
                  "game_profiles": {
                      "100": {"spl": 8, "sppt": 10, "fppt": 12, "preset": "Low power"},
                      "200": {"spl": 5, "sppt": 8, "fppt": 10, "preset": "Minimum"},
                      "300": {"spl": 32, "sppt": 35, "fppt": 37, "preset": "Max"},
                      "bad id!": {"spl": 15, "sppt": 18, "fppt": 25, "preset": "Balanced"},
                      "400": "corrupt"}}
        self.assertTrue(tdp._migrate(legacy))
        state = tdp._load_settings()
        self.assertEqual((state["spl"], state["sppt"], state["fppt"], state["active_preset"]),
                         (30000, 43000, 45000, "performance"))
        self.assertIs(state["cpu_boost_enabled"], False)
        profiles = tdp._load_profiles()
        self.assertEqual(set(profiles), {"100", "200", "300"})
        self.assertEqual({app: (p["spl"], p["sppt"], p["fppt"], p["preset"]) for app, p in profiles.items()}, {
            "100": (10000, 15000, 20000, "silent"),
            "200": (5000, 8000, 10000, "custom"),
            "300": (30000, 35000, 37000, "custom"),  # SPL above the SMU's 30 W never ran anyway
        })
        baseline = self.store.getSetting("baseline")
        self.assertTrue(baseline["pending"])
        self.assertIn("after_boot", baseline, "1.2.x may have written CPU Boost: wait for a reboot")

    def test_legacy_settings_never_replace_a_store_that_already_exists(self):
        self.seed(spl=10000, sppt=15000, fppt=20000, active_preset="silent")
        self.assertFalse(tdp._migrate({"tdp": {"spl": 30, "sppt": 32, "fppt": 35},
                                       "tdp_preset": "Performance"}))
        self.assertEqual(tdp._load_settings()["active_preset"], "silent")
        self.assertFalse(tdp._migrate({}))

    def test_migration_hook_forgets_the_old_keys_only_after_the_import(self):
        forgotten = []
        plugin = tdp.Plugin(lambda: {"tdp_preset": "Balanced", "tdp": {"spl": 15, "sppt": 18, "fppt": 25}},
                            lambda: forgotten.append(True))
        asyncio.run(plugin._migration())
        self.assertEqual(forgotten, [True])
        self.assertEqual(tdp._load_settings()["sppt"], 25000)
        asyncio.run(plugin._migration())
        self.assertEqual(forgotten, [True], "nothing left to carry on the next start")

    # The RyzenAdj helper -------------------------------------------------------------

    def test_helper_is_downloaded_verified_and_learns_whether_limits_read_back(self):
        self.assertFalse(tdp._ensure_ryzenadj(download=False))
        self.assertEqual(self.downloads, 0)
        self.readback = False
        self.assertTrue(tdp._ensure_ryzenadj())
        self.assertEqual((self.downloads, (self.bin_dir / "ryzenadj").read_bytes()), (1, HELPER))
        self.assertFalse(tdp._readback_available, "a blocked power table still allows writes")
        self.readback = True
        self.assertTrue(tdp._ensure_ryzenadj(download=False))
        self.assertTrue(tdp._readback_available)
        self.assertEqual(self.downloads, 1)

    def test_a_tampered_archive_or_binary_installs_nothing(self):
        for name, data in (("archive", b"not the pinned archive"),
                           ("binary", archive(payload=b"\x7fELF something else"))):
            with self.subTest(name), patch.object(tdp, "RYZENADJ_SHA256", hashlib.sha256(data).hexdigest()
                                                  if name == "binary" else tdp.RYZENADJ_SHA256):
                self.archive = data
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    tdp._ensure_ryzenadj()
                self.assertFalse((self.bin_dir / "ryzenadj").exists())
                self.assertEqual(sorted(os.listdir(self.bin_dir)), [])

    def test_a_cancelled_download_installs_nothing(self):
        with self.assertRaises(InterruptedError):
            tdp._ensure_ryzenadj(cancel=lambda: self.downloads > 0)
        self.assertFalse((self.bin_dir / "ryzenadj").exists())

    @POSIX
    def test_a_helper_others_can_change_is_never_run(self):
        tdp._ensure_ryzenadj()
        os.chmod(self.bin_dir / "ryzenadj", 0o777)
        self.assertFalse(tdp._helper_verified())
        tdp._ryzenadj_available = True
        self.assertEqual(tdp._run_ryzenadj(["--info"])[0], -1)

    def test_start_downloads_a_missing_helper_in_the_background_then_applies(self):
        self.seed(spl=10000, sppt=15000, fppt=20000, active_preset="silent")
        plugin = tdp.Plugin()

        async def scenario():
            with patch.object(plugin, "_enforce_loop", AsyncMock()), \
                 patch.object(plugin, "_info_loop", AsyncMock()):
                tdp.module_runtime.begin_start("tdp")  # as main does before every start
                await plugin._main()
                self.assertTrue(plugin._ready, plugin._setup_error)
                self.assertEqual(self.writes, [], "nothing to write with until the helper is verified")
                await asyncio.gather(*plugin._tasks)
            self.assertTrue(tdp._ryzenadj_available)
            tdp._check_and_enforce()
            self.assertEqual(self.writes, [(10000, 15000, 20000)])
            await plugin._unload()
        asyncio.run(scenario())

    # Ownership and writes ------------------------------------------------------------

    def test_powerstation_ownership_blocks_every_write(self):
        tdp._ryzenadj_available = True
        with patch.object(tdp, "_powerstation_active", return_value=True):
            result = tdp._apply_limits(15000, 25000, 30000)
            caps = asyncio.run(tdp.Plugin().get_caps())
        self.assertFalse(result["success"])
        self.assertIn("PowerStation", result["stderr"])
        self.assertEqual((caps["tdp_available"], caps["owner"]), (False, "PowerStation"))
        with patch.object(tdp, "_powerstation_active", side_effect=RuntimeError("busctl hung")):
            self.assertIn("Could not verify", tdp._apply_limits(15000, 25000, 30000)["stderr"])
        self.assertEqual(self.writes, [])

    def test_writes_are_clamped_and_need_the_verified_helper(self):
        self.assertIn("unavailable", tdp._apply_limits(15000, 25000, 30000)["stderr"])
        tdp._ensure_ryzenadj()
        tdp._ryzenadj_available = True
        self.assertTrue(tdp._apply_limits(50000, 50000, 50000)["success"])
        self.assertEqual(self.writes, [(30000, 43000, 45000)])
        self.assertEqual(tdp._applied_mw, (30000, 43000, 45000))

    def test_caps_report_range_presets_and_readback(self):
        tdp._ensure_ryzenadj()
        tdp._ryzenadj_available = True
        caps = asyncio.run(tdp.Plugin().get_caps())
        self.assertEqual(caps, {"min": 5, "max": {"spl": 30, "sppt": 43, "fppt": 45},
                                "tdp_available": True, "tdp_error": "", "owner": "RyzenAdj",
                                "readback": True, "helper_retry_available": False,
                                "presets": tdp.PRESETS})
        tdp._ryzenadj_available = False
        caps = asyncio.run(tdp.Plugin().get_caps())
        self.assertEqual((caps["tdp_available"], caps["helper_retry_available"]), (False, True))

    # The SMU's charger-change clamp ---------------------------------------------------

    def ready(self, readback):
        self.readback = readback
        tdp._ensure_ryzenadj()
        tdp._ryzenadj_available = True
        self.seed(spl=30000, sppt=43000, fppt=45000, active_preset="performance")
        tdp._current_game_id = "\x00"  # as after start: the first pass applies the saved target
        tdp._check_and_enforce()
        self.assertEqual(self.smu, [30.0, 43.0, 45.0])
        self.writes.clear()

    def unplug(self):
        """What the AYANEO firmware does: the SMU drops SPL to 28 W on a power-source change."""
        self.ac = not self.ac
        self.smu[0] = min(self.smu[0], 28.0)

    def test_a_charger_change_rewrites_the_target_without_readback(self):
        self.ready(readback=False)
        self.unplug()
        with patch.object(tdp.time, "monotonic", return_value=tdp.time.monotonic()):
            tdp._check_and_enforce()
        self.assertEqual(self.writes, [(30000, 43000, 45000)])
        self.assertEqual(self.smu, [30.0, 43.0, 45.0])
        # Without a readback every settle step writes again: the firmware may act late.
        generation = tdp._ac_generation
        for _ in tdp.AC_SETTLE_DELAYS_S:
            self.assertFalse(tdp._reapply_current_target(generation))
        self.assertEqual(len(self.writes), 1 + len(tdp.AC_SETTLE_DELAYS_S))

    def test_with_readback_the_settle_ladder_stops_once_the_smu_agrees(self):
        self.ready(readback=True)
        self.unplug()
        tdp._check_and_enforce()
        self.assertEqual(self.smu, [30.0, 43.0, 45.0])
        tdp._invalidate_limits_cache()
        self.assertTrue(tdp._reapply_current_target(tdp._ac_generation))
        self.assertEqual(len(self.writes), 1, "a matching readback needs no second write")

    def test_readback_drift_catches_the_28_w_clamp_and_nothing_else(self):
        self.ready(readback=True)
        tdp._check_and_enforce()
        self.assertEqual(self.writes, [], "the SMU holds the target: no write")
        self.smu[0] = 28.0
        tdp._invalidate_limits_cache()
        tdp._check_and_enforce()
        self.assertEqual(self.writes, [(30000, 43000, 45000)])
        self.readback = False
        tdp._readback_available = False
        self.smu[0] = 28.0
        tdp._check_and_enforce()
        self.assertEqual(len(self.writes), 1, "without a readback only power-source changes rewrite")

    def test_turning_tdp_control_off_stops_writing(self):
        self.ready(readback=False)
        result = asyncio.run(tdp.Plugin().set_plugin_enabled(False))
        self.assertTrue(result["success"], result)
        self.unplug()
        tdp._check_and_enforce()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.smu[0], 28.0, "the firmware's own value stays")


if __name__ == "__main__":
    unittest.main()
