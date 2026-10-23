import faulthandler
import os, sys, tempfile, types

# A hung run ends with every thread's stack instead of stalling (the suite takes ~40 s, CI allows 10 min).
faulthandler.dump_traceback_later(480, exit=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import diagnostics  # noqa: E402  (no I/O at import)
# No file sink unless a test injects its own directory: never the real /var/log.
DEFAULT_LOG_DIR, diagnostics.LOG_DIR = diagnostics.LOG_DIR, None


class _Socket:  # Shape of Decky's UnixSocket, so main's unload workaround finds its listener.
    async def _read_single_line(self, _reader):
        return ""


def _emitter(sandboxed):
    async def emit(_event, *_args):
        sandboxed._socket
    return emit


def install():
    temp = tempfile.mkdtemp(prefix="aya3-tests-")
    decky = types.ModuleType("decky")
    decky.DECKY_PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
    decky.DECKY_PLUGIN_SETTINGS_DIR = temp
    decky.logger = types.SimpleNamespace(info=lambda *_: None, warning=lambda *_: None,
                                        error=lambda *_: None, debug=lambda *_: None)
    decky.emit = _emitter(types.SimpleNamespace(_socket=_Socket()))
    sys.modules["decky"] = decky
    settings = types.ModuleType("settings")
    class SettingsManager:
        def __init__(self, **_): self.data = {}
        def read(self): return self.data
        def getSetting(self, key, default=None): return self.data.get(key, default)
        def setSetting(self, key, value): self.data[key] = value
        def commit(self): pass
    settings.SettingsManager = SettingsManager
    sys.modules["settings"] = settings


# Backend modules in lookup order; main comes last because it imports from them.
MODULES = ("companion_common", "ec_backend", "controller_backend", "power_backend",
           "audio_backend", "display_backend", "input_backend", "main")


def owner(name):
    """Return the module that defines `name`, so a patch reaches every caller."""
    import importlib
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
