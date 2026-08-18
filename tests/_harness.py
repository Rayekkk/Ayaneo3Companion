import os, sys, tempfile, types

def install():
    temp = tempfile.mkdtemp(prefix="aya3-tests-")
    decky = types.ModuleType("decky")
    decky.DECKY_PLUGIN_DIR = os.path.dirname(os.path.dirname(__file__))
    decky.DECKY_PLUGIN_SETTINGS_DIR = temp
    decky.logger = types.SimpleNamespace(info=lambda *_: None, warning=lambda *_: None, error=lambda *_: None)
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
