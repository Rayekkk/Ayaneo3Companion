# SPDX-License-Identifier: BSD-3-Clause
"""Ayaneo test setup on top of the shared Decky harness: import it before any backend module.

Isolates what the suite must never touch, also when it runs on the console itself: the diagnostics log in
/var/log, the gamescope definitions, the RyzenAdj helper and its download.
"""
import importlib
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _harness  # noqa: E402,F401  (the shared decky stand-in; puts the plugin folder on sys.path)
import decky  # noqa: E402

import diagnostics  # noqa: E402  (no I/O at import)

# No file sink unless a test injects its own directory: never the real /var/log.
DEFAULT_LOG_DIR, diagnostics.LOG_DIR = diagnostics.LOG_DIR, None


class _Socket:  # Shape of Decky's UnixSocket, so main's unload workaround finds its listener.
    async def _read_single_line(self, _reader):
        return ""


def _emitter(sandboxed):
    async def emit(event, *args):
        sandboxed._socket
        decky.emitted.append((event, *args))
    return emit


decky.emit = _emitter(types.SimpleNamespace(_socket=_Socket()))

_temp = Path(decky.DECKY_PLUGIN_SETTINGS_DIR).parent
import display_backend  # noqa: E402

_display = _temp / "display"
display_backend.CONFIG_DIR = _display / "config"
display_backend.LUA_TARGET = _display / "config" / "gamescope" / "scripts" / display_backend.SCRIPT_NAME
display_backend.LEGACY_LUA_TARGET = _display / "etc" / display_backend.SCRIPT_NAME
display_backend.NATIVE_LUA = _display / "usr" / display_backend.SCRIPT_NAME
display_backend.SCRIPT_USER = None

# A test that starts TDP Control gets an empty helper folder and a download that always fails.
import tdp_backend  # noqa: E402

tdp_backend.BIN_DIR = str(_temp / "ryzenadj")
tdp_backend.BIN_PATH = str(_temp / "ryzenadj" / "ryzenadj")


def _no_download(*_args, **_kwargs):
    raise RuntimeError("tests never download RyzenAdj")


REAL_DOWNLOAD_ARCHIVE = tdp_backend._download_archive  # for tests of the transfer itself
tdp_backend._download_archive = _no_download

# Backend modules in lookup order; main follows the modules it imports from. The ported
# modules come after it: their names (settings, _ensure_ryzenadj, ...) shadow older ones.
MODULES = ("companion_common", "ec_backend", "controller_backend",
           "audio_backend", "display_backend", "input_backend", "main", "active_app", "tdp_backend",
           "fan_sleep", "fan_policy", "fan_hwmon", "fan_guardian", "fan_backend")


def owner(name):
    """Return the module that defines `name`, so a patch reaches every caller."""
    for module_name in MODULES:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name:
                raise
            continue
        if name in vars(module):
            return module
    raise AttributeError(f"no Companion module defines {name!r}")


import controller_backend  # noqa: E402


def assert_refused(test, result, pattern="."):
    """A RuntimeError/ValueError the backend raised, answered as D2: its text for the user and nothing else."""
    test.assertEqual(set(result), {"ok", "error"})
    test.assertIs(result["ok"], False)
    test.assertNotRegex(result["error"], "^Unexpected error")
    test.assertRegex(result["error"], pattern)


def controller_report(config):
    """The AYA_CHECK answer of a controller running `config`."""
    packet = controller_backend.controller_command(config)
    response = bytearray(64)
    response[3] = controller_backend.AYA_CHECK[4]
    response[7:15] = packet[8:16]
    response[21:24] = packet[22:25]
    return bytes(response)
