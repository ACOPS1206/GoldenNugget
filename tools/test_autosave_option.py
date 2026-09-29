#!/usr/bin/env python3
"""Offline test for the "Save Tweaks Automatically" option (no device needed).

AutoSave is a built-in preset the app wrote on every tweak change and read
back at startup, so the tweak UI survived a restart. There was no way to turn
that off, and a fresh `AutoSave` was written over the old one on every launch
regardless of what the user wanted.

The option stops BOTH halves: no writes while tweaks change, and no AutoSave
load at startup (it then falls back to the last manually-loaded preset, like a
fresh install). The preset file itself is deliberately left alone so an
existing one stays loadable by hand.

Note: the CLI (`nugget apply` / `nugget tweaks set`) drives AutoSave through
src/cli/common.py and is intentionally NOT gated by this GUI pref -- it is a
separate surface with its own --no-save flags and no session state to fall back
on. This test only pins the GUI behaviour.

Run: python tools/test_autosave_option.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.gui.main_window_mixins import SettingsMixin

PASS = 0


def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAILED: {name} {extra}"
    PASS += 1
    print(f"  ok: {name}" + (f"  [{extra}]" if extra else ""))


class FakeSettings:
    """Just enough QSettings for the two keys the mixin reads/writes."""

    def __init__(self, last_loaded_preset=""):
        self.values = {"last_loaded_preset": last_loaded_preset}
        self.synced = 0

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced += 1


class FakePresetManager:
    def __init__(self, names=()):
        self.names = list(names)
        self.loaded = []
        self.saved = []

    def list_presets(self):
        return list(self.names)

    def load_preset(self, name):
        self.loaded.append(name)
        return True

    def save_preset(self, name, desc, tags=None, device_model="", ios_version=""):
        self.saved.append(name)
        return True


class FakeDeviceManager:
    def __init__(self, autosave=True):
        self.pref_manager = type("P", (), {"tweak_autosave": autosave})()

    def get_current_device_model(self):
        return "iPhone16,1"

    def get_current_device_version(self):
        return "27.0.1"


def make_window(autosave=True, presets=("AutoSave",), last_loaded_preset=""):
    win = SettingsMixin()
    win.settings = FakeSettings(last_loaded_preset)
    win.preset_manager = FakePresetManager(presets)
    win.device_manager = FakeDeviceManager(autosave)
    win._preset_autosave_pending = False
    return win


# =============================================================================
def test_option_state():
    print("\nthe option itself")
    check("enabled by default", make_window().autosave_enabled() is True)
    check("can be turned off", make_window(autosave=False).autosave_enabled() is False)

    class Broken:
        device_manager = None
    w = SettingsMixin()
    w.device_manager = Broken()
    check("a broken pref falls back to on (old behaviour)",
          w.autosave_enabled() is True)


# =============================================================================
def test_writes_are_gated():
    print("\ntweak changes do not write AutoSave while off")
    win = make_window(autosave=False)
    win._on_tweak_changed()
    check("no debounce timer is even scheduled",
          win._preset_autosave_pending is False)
    check("nothing was written", win.preset_manager.saved == [])

    # a timer scheduled before the switch was flipped off still fires
    win._preset_autosave_pending = True
    win._save_autosave_preset()
    check("a timer left over from before the switch cannot resurrect the file",
          win.preset_manager.saved == [])
    check("the pending flag is cleared anyway",
          win._preset_autosave_pending is False)

    on = make_window(autosave=True)
    on._on_tweak_changed()
    check("while on, a change does schedule the save",
          on._preset_autosave_pending is True)
    on._save_autosave_preset()
    check("while on, AutoSave is written", on.preset_manager.saved == ["AutoSave"])
    check("the pending flag is cleared after the write",
          on._preset_autosave_pending is False)


# =============================================================================
def test_startup_load():
    print("\nstartup does not load AutoSave while off")
    off = make_window(autosave=False, last_loaded_preset="My Setup")
    off._load_last_preset()
    check("AutoSave is NOT loaded", "AutoSave" not in off.preset_manager.loaded,
          str(off.preset_manager.loaded))
    check("it falls back to the last manual preset",
          off.preset_manager.loaded == ["My Setup"], str(off.preset_manager.loaded))
    check("the existing AutoSave file is left alone",
          "AutoSave" in off.preset_manager.names)

    bare = make_window(autosave=False, last_loaded_preset="")
    bare._load_last_preset()
    check("with nothing to fall back to, nothing is loaded",
          bare.preset_manager.loaded == [])

    on = make_window(autosave=True)
    on._load_last_preset()
    check("while on, AutoSave still loads at startup",
          on.preset_manager.loaded == ["AutoSave"], str(on.preset_manager.loaded))
    check("and is rewritten right away to purge stale entries",
          on.preset_manager.saved == ["AutoSave"])

    on_fallback = make_window(autosave=True, presets=(), last_loaded_preset="My Setup")
    on_fallback._load_last_preset()
    check("while on, a missing AutoSave still falls back",
          on_fallback.preset_manager.loaded == ["My Setup"])


# =============================================================================
def test_banner_and_settings_hook():
    print("\nthe UI tells the truth about it")
    from src.gui.ios import settings as settings_mod
    from src.gui.preset_widget import PresetBanner

    banner = PresetBanner.__new__(PresetBanner)
    seen = []
    banner.active_lbl = type("L", (), {"setText": lambda _s, t: seen.append(t)})()

    banner.set_active_preset("My Setup", autosave=False)
    check("a loaded preset is always named", seen[-1] == "My Setup")

    banner.set_active_preset("", autosave=True)
    check("no preset + autosave on says AutoSave", seen[-1] == "AutoSave")

    banner.set_active_preset("", autosave=False)
    check("no preset + autosave off must not claim AutoSave",
          seen[-1] != "AutoSave", seen[-1])
    check("it says the state is not saved", seen[-1] == "Not Saved", seen[-1])

    src = open(settings_mod.__file__, encoding="utf-8").read()
    check("the Presets section has the switch", "_on_autosave_toggled" in src)
    check("it persists to the same key the loader reads",
          '"tweak_autosave"' in src)

    import inspect
    loader = inspect.getsource(SettingsMixin.loadSettings)
    check("the pref is loaded with autosave ON as the default",
          'value("tweak_autosave", True, type=bool)' in loader)
    check("and is assigned to the pref manager",
          "pref_manager.tweak_autosave = tweak_autosave" in loader)


def main():
    test_option_state()
    test_writes_are_gated()
    test_startup_load()
    test_banner_and_settings_hook()
    print(f"\nALL {PASS} CHECKS PASSED")


if __name__ == "__main__":
    main()
