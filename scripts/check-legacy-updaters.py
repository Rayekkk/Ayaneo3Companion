#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause
"""Check that a release ZIP is accepted by the in-plugin updaters users already run.

Usage: python scripts/check-legacy-updaters.py artifacts/Ayaneo3Companion-X.Y.Z.zip

Loads lego_updater.py from the 1.1.0, 1.2.0 and 1.3.0 tags (needs the tags fetched)
and runs the same checks those versions run on a downloaded release: the asset
size limit and Updater._validate_archive. Earlier releases did not validate.
"""
import json
from pathlib import Path
import subprocess
import sys
import types
import zipfile

TAGS = ("1.1.0", "1.2.0", "1.3.0")
ROOT = Path(__file__).resolve().parent.parent


def legacy_updater(tag):
    source = subprocess.run(["git", "show", f"{tag}:lego_updater.py"], cwd=ROOT,
                            check=True, capture_output=True, text=True).stdout
    module = types.ModuleType(f"lego_updater_{tag.replace('.', '_')}")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{tag}:lego_updater.py", "exec"), module.__dict__)
    return module


def main(argv):
    if len(argv) != 2:
        sys.exit(__doc__.strip().splitlines()[2])
    path = Path(argv[1])
    size = path.stat().st_size
    with zipfile.ZipFile(path) as archive:
        version = json.loads(archive.read("Ayaneo3Companion/plugin.json"))["version"]
    failed = False
    for tag in TAGS:
        updater = legacy_updater(tag)
        try:
            if not 0 < size <= updater.MAX_DOWNLOAD:  # the size gate in _parse_release
                raise ValueError(f"The release ZIP is {size} bytes; the limit is {updater.MAX_DOWNLOAD}.")
            with path.open("rb") as stream:
                updater.Updater._validate_archive(stream, version)
        except Exception as error:  # any rejection, including a malformed ZIP
            failed = True
            print(f"FAIL {tag} updater rejects {path.name}: {type(error).__name__}: {error}")
        else:
            print(f"OK   {tag} updater accepts {path.name} ({version}, {size} bytes)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
