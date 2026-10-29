# SPDX-License-Identifier: BSD-3-Clause
"""A new release stays installable by what users already run: the in-plugin updaters of 1.1.0 to 1.3.0, the
offline bootstrap.sh of 1.2.0 and 1.3.0, and the plugin's own updater (dk_update; its guarantees: test_dk_update).
scripts/check-legacy-updaters.py runs the old updaters themselves on a built ZIP (CI: legacy.yml)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import support  # noqa: F401  (the Decky harness before any backend import)
import bootstrap_installer
import dk_update
import main

ROOT = Path(__file__).resolve().parents[1]
# lego_updater.REQUIRED_RUNTIME_FILES of 1.3.0, a superset of 1.1.0's and 1.2.0's: checked by name, not empty.
LEGACY_REQUIRED = (
    "main.py", "plugin.json", "package.json",
    "safe_settings.py", "quality_runtime.py", "lego_updater.py", "fan_backend.py", "fan_sleep.py", "fan_guardian.py",
    "fan_hwmon.py", "fan_policy.py", "active_app.py", "tdp_backend.py", "display_script_io.py", "module_runtime.py",
    "diagnostics.py", "system_process.py", "input_backend.py", "display_backend.py", "audio_backend.py",
    "controller_backend.py", "ec_backend.py", "companion_common.py",
    "assets/ayaneo.ayaneo3.oled.lua", "assets/ayaneo3-companion.yaml",
)


def manifests():
    return (json.loads((ROOT / "plugin.json").read_text(encoding="utf-8")),
            json.loads((ROOT / "package.json").read_text(encoding="utf-8")))


class ReleaseCompatibilityTests(unittest.TestCase):
    def test_files_older_updaters_require_by_name_ship_and_are_not_empty(self):
        _, package = manifests()
        listed = package["decky"]["files"]
        for name in LEGACY_REQUIRED:
            with self.subTest(name=name):
                self.assertGreater((ROOT / name).stat().st_size, 0)
                self.assertTrue(name in listed or name.split("/")[0] in listed
                                or (name.endswith(".py") and "/" not in name and "*.py" in listed))

    def test_a_release_missing_any_module_the_plugin_starts_with_is_refused(self):
        required = set(bootstrap_installer.REQUIRED)
        self.assertLessEqual(set(LEGACY_REQUIRED), required)
        modules = {p.name for p in ROOT.glob("*.py")} - {"bootstrap_installer.py"}  # the installer runs on its own
        self.assertEqual(modules - required, set(), "add the new module to bootstrap_installer.REQUIRED")
        self.assertEqual(main._updates().required, bootstrap_installer.REQUIRED)

    def test_the_updater_is_this_plugins_and_asks_for_files_that_ship(self):
        manifest, package = manifests()
        updates = main._updates()
        self.assertEqual((updates.repo, updates.folder, updates.plugin_name, updates.package_name, updates.flags),
                         ("Rayekkk/Ayaneo3Companion", package["decky"]["folder"], manifest["name"], package["name"],
                          manifest["flags"]))
        for required in (*updates.required, *bootstrap_installer.REQUIRED):
            if required != "dist/index.js":  # built, not in git
                self.assertTrue((ROOT / required).is_file(), required)

    def test_an_older_bootstrap_sh_can_still_check_and_install_a_new_zip(self):
        """bootstrap.sh of 1.2.0/1.3.0 extracts only bootstrap_installer.py and lego_updater.py; the new installer
        takes the updater's ZIP check (dk_update and the modules it imports) from the same ZIP."""
        manifest, package = manifests()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            archive = folder / "Ayaneo3Companion-9.9.9.zip"
            files = {name: (ROOT / name).read_bytes() for name in bootstrap_installer.REQUIRED
                     if name != "dist/index.js"}
            files.update({"dist/index.js": b"bundle", "lego_updater.py": (ROOT / "lego_updater.py").read_bytes(),
                          "plugin.json": json.dumps({**manifest, "version": "9.9.9"}).encode(),
                          "package.json": json.dumps({**package, "version": "9.9.9"}).encode()})
            with zipfile.ZipFile(archive, "w") as payload:
                for name, data in files.items():
                    payload.writestr(f"Ayaneo3Companion/{name}", data)
            old_work_dir = folder / "work"
            old_work_dir.mkdir()
            for name in ("bootstrap_installer.py", "lego_updater.py"):
                shutil.copy(ROOT / name, old_work_dir / name)
            check = ("import sys; sys.path.insert(0, sys.argv[1]); import bootstrap_installer; "
                     "print(bootstrap_installer.validate_archive(sys.argv[2]))")
            result = subprocess.run([sys.executable, "-I", "-c", check, str(old_work_dir), str(archive)],
                                    capture_output=True, text=True, timeout=60, cwd=temporary)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "9.9.9"), result.stderr[-1500:])
            self.assertEqual(sorted(p.name for p in old_work_dir.iterdir() if p.suffix == ".py"),
                             sorted(["bootstrap_installer.py", "lego_updater.py", *bootstrap_installer.VALIDATOR]))

    def test_bootstrap_sh_extracts_the_installer_and_its_validator(self):
        script = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        for name in ("bootstrap_installer.py", *bootstrap_installer.VALIDATOR):
            self.assertIn(f'"{name}"', script)

    def test_the_package_ships_decky_apis_licence_and_names_it(self):
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        api = lock["packages"]["node_modules/@decky/api"]["version"]
        self.assertIn(f"Decky Loader API {api}", (ROOT / "NOTICE").read_text(encoding="utf-8"))
        self.assertIn("third_party/decky-api", (ROOT / "NOTICE").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
