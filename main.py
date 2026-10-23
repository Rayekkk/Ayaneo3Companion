# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
# https://github.com/Rayekkk/Ayaneo3Companion

"""AYANEO 3 hardware controls for Decky Loader."""

import asyncio
import copy
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import decky

# The plugin's own modules are not importable until its directory is on sys.path.
_plugin_path = str(Path(decky.DECKY_PLUGIN_DIR))
if _plugin_path not in sys.path:
    sys.path.insert(0, _plugin_path)

from safe_settings import SettingsManager  # noqa: E402
from quality_runtime import complete, guard_public_calls  # noqa: E402
from fan_backend import FanController, FAN_RENEW_SECONDS  # noqa: E402
import audio_backend  # noqa: E402
import companion_common  # noqa: E402
import controller_backend  # noqa: E402
import display_backend  # noqa: E402
import ec_backend  # noqa: E402
import input_backend  # noqa: E402
import power_backend  # noqa: E402
from companion_common import LOG  # noqa: E402

TM_GUARD_INTERVAL = 0.5
CONTROLLER_GAIN_CHECK_INTERVAL = 2.0
CONTROLLER_RESUME_THRESHOLD = 1.0
TDP_DIAGNOSTIC_INTERVAL = 5.0
TDP_DIAGNOSTIC_FIELDS = ("tdp_backend", "tdp_supported", "tdp_error")
# Only diagnostic fields are remembered by a status read. Applied values and
# game identity remain owned by their transactions and background monitors.
SNAPSHOT_DIAGNOSTICS = (
    "gpu_power_w", "tdp_backend", "tdp_supported", "tdp_error",
    "controller_supported", "controller_error", "cpu_boost_supported",
    "screen_conflict", "edid_patched", "edid_game_nits",
    "charge_bypass_supported", "module_eject_supported", "module_reset_supported",
)
VIBRATION_TEST_MS = 500

settings = SettingsManager(name="settings", settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR)
fan_controller = FanController()
_lock = threading.RLock()
_tdp_mutation_lock = threading.RLock()
_charge_mutation_lock = threading.RLock()


def _systemctl(*arguments, check: bool = False):
    return subprocess.run(["/usr/bin/systemctl", *arguments], check=check,
                          capture_output=True, text=True, timeout=20,
                          env=companion_common.clean_subprocess_env())


def _suspend_clock_offset() -> float | None:
    """Return a value that increases only while Linux is suspended."""
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


@guard_public_calls
class Plugin:
    _state = {}
    _tdp_observation_epoch = 0
    _tdp_writes = 0
    _restore_task = None
    _edid_task = None
    _ac_task = None
    _charge_task = None
    _module_task = None
    _tm_guard_task = None
    _vibration_gain_task = None
    _audio_task = None
    _fan_task = None
    _active_app = ""

    def __init__(self):
        self._closing = False
        self._initializing = False
        self._rpc_lock = asyncio.Lock()
        # Fan transactions do not mutate persisted settings or SMU/controller
        # state. Keep Auto available while those unrelated operations run.
        self._fan_rpc_lock = asyncio.Lock()
        self._fan_generation = 0
        self._rpc_owner = None
        self._rpc_jobs = set()
        self._workers = set()
        self._audio_mutation_lock = threading.RLock()
        self._startup_task = None
        self._cleanup_task = None
        self._tdp_diagnostic_task = None
        self._tdp_diagnostic_after = 0.0

    def _check_write_allowed(self):
        error = getattr(settings, "recovery_error", "")
        if error:
            raise RuntimeError(f"Settings need recovery: {error}")
        if self._initializing:
            raise RuntimeError("Plugin is still starting; try again shortly")
        if Plugin._state.get("startup_error"):
            raise RuntimeError(Plugin._state["startup_error"])
        if Plugin._state.get("supported") is False:
            raise RuntimeError("This device is not an AYANEO 3")

    async def _offload(self, function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._workers.add(task)
        task.add_done_callback(self._workers.discard)
        return await complete(task)

    @staticmethod
    def _boolean(value):
        if type(value) is not bool:
            raise ValueError("Expected a boolean value")
        return value

    @staticmethod
    def _app_id(value):
        if value is None or value == "":
            return ""
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None:
            raise ValueError("Invalid game ID")
        return value

    @classmethod
    def _check_game(cls, expected_app_id):
        if expected_app_id is not None and cls._app_id(expected_app_id) != cls._active_app:
            raise RuntimeError("game is no longer active")

    @staticmethod
    def _save_values(values):
        previous = copy.deepcopy(settings.data)
        try:
            for key, value in values.items():
                settings.setSetting(key, value)
            settings.commit()
        except Exception:
            settings.data = previous
            raise

    @staticmethod
    def _apply_and_save(apply, restore, values):
        try:
            apply()
            Plugin._save_values(values)
        except power_backend.TdpUnavailableError:
            # This capability rejection occurs before any device write.
            raise
        except Exception as error:
            try:
                restore()
            except Exception as rollback_error:
                raise RuntimeError(f"{error}; hardware rollback failed: {rollback_error}") from error
            raise RuntimeError(f"{error}; previous hardware settings restored") from error

    @classmethod
    def _apply_tdp_target(cls, target):
        with _lock:
            cls._tdp_writes += 1
            cls._tdp_observation_epoch += 1
        try:
            power_backend.apply_tdp(target)
            boost = settings.getSetting("cpu_boost", None)
            if type(boost) is bool:
                power_backend.write_cpu_boost(boost)
        except power_backend.TdpUnavailableError as error:
            with _lock:
                owner = ("PowerStation" if str(error) == power_backend.POWERSTATION_TDP_ERROR
                         else "Unavailable")
                cls._state.update(tdp_backend=owner, tdp_supported=False, tdp_error=str(error))
            raise
        else:
            with _lock:
                cls._state.update(tdp_backend="RyzenAdj", tdp_supported=True, tdp_error="")
        finally:
            # Invalidate older observations even when a successful write leaves
            # the cached capability values equal, or ownership rejects a write.
            with _lock:
                cls._tdp_writes -= 1
                cls._tdp_observation_epoch += 1

    @classmethod
    def _tdp_rollback(cls, target):
        try:
            boost = power_backend.read_cpu_boost()
        except (OSError, RuntimeError):
            boost = None
        def restore():
            try:
                cls._apply_tdp_target(target)
            finally:
                if type(boost) is bool:
                    power_backend.write_cpu_boost(boost)
        return restore

    @staticmethod
    def _save(key, value):
        Plugin._save_values({key: value})

    @classmethod
    def _reapply_current_tdp(cls):
        """Apply this game's desired limits, including a deferred profile."""
        with _tdp_mutation_lock:
            with _lock:
                target = dict(cls._state["tdp"])
            cls._apply_tracked_tdp(target)
            return target

    @classmethod
    def _apply_tracked_tdp(cls, target):
        """Keep requested game context separate from the last applied limits."""
        with _lock:
            previous = dict(cls._state.get("_applied_tdp", cls._state["tdp"]))
        restore = cls._tdp_rollback(previous)
        try:
            cls._apply_tdp_target(target)
        except power_backend.TdpUnavailableError as error:
            # Ownership rejection happens before a write. Keep the current
            # game's target pending so a later availability change can apply it.
            with _lock:
                cls._state.update(tdp_profile_pending=True,
                                  tdp_profile_error=str(error),
                                  tdp_supported=False, tdp_error=str(error))
            raise
        except Exception as error:
            try:
                restore()
            except Exception as recovery:
                error = RuntimeError(f"{error}; TDP rollback failed: {recovery}")
            with _lock:
                cls._state.update(tdp_profile_pending=True,
                                  tdp_profile_error=str(error))
            raise error
        with _lock:
            cls._state.update(tdp_profile_pending=False, tdp_profile_error="",
                              tdp_supported=True, tdp_error="", _applied_tdp=dict(target))

    @classmethod
    def _reconcile_current_controller(cls, recover_custom: bool,
                                      force: bool = False):
        """Read the newest desired state after all earlier writes complete."""
        with controller_backend.controller_apply_lock:
            with _lock:
                if cls._state.get("modules_reconnecting"):
                    raise RuntimeError("Controller module operation is in progress")
                controller = dict(cls._state["controller"])
                recover_custom = cls._state.get("tm_guard_enabled", recover_custom)
                owned = cls._state.get("controller_owned", True)
                seed = not owned and not cls._state.get("controller_seeded")
            if seed:
                # Until the user changes a setting, show the controller's own
                # lighting and vibration instead of writing Companion defaults.
                controller = {**controller_backend.read_controller(),
                              "ff_gain": controller["ff_gain"]}
                with _lock:
                    cls._state.update(controller=dict(controller), controller_seeded=True)
            status, path = controller_backend.reconcile_controller(
                controller, recover_custom, force, repair=owned)
            return status, path

    @classmethod
    def _restore_current_controller(cls, persist_firmware: bool = False):
        """Atomically snapshot and restore all controller-side settings."""
        with controller_backend.controller_apply_lock:
            with _lock:
                if (cls._state.get("modules_reconnecting")
                        and not cls._state.get("modules_detached")):
                    raise RuntimeError("Waiting for the requested module operation")
                if not cls._state.get("controller_owned", True):
                    return None
                controller = dict(cls._state["controller"])
            controller_backend.apply_controller(controller, persist_firmware=persist_firmware)
            controller_backend.set_vibration_gain(controller["ff_gain"])
            return controller

    @classmethod
    def _restore_rear_buttons(cls):
        """Program LC1/RC1 for the newest key-binding choice after a reinstall."""
        with controller_backend.controller_apply_lock:
            with _lock:
                if cls._state.get("modules_reconnecting"):
                    raise RuntimeError("Waiting for the requested module operation")
                if not cls._state.get("button_fix_installed"):
                    return
                controller = dict(cls._state["controller"])
            controller_backend.program_rear_buttons(True, controller)

    @classmethod
    def _restore_current_vibration_gain(cls):
        with controller_backend.controller_apply_lock:
            with _lock:
                if (cls._state.get("modules_reconnecting", False)
                        or not cls._state.get("modules_connected", False)):
                    raise RuntimeError("Waiting for controller modules")
                if not cls._state.get("controller_owned", True):
                    return None
                gain = cls._state["controller"]["ff_gain"]
            controller_backend.set_vibration_gain(gain)
            return gain

    @classmethod
    def _copy_state(cls):
        """Acknowledge confirmed writes without unrelated hardware probes."""
        with _lock:
            state = dict(cls._state)
            state.pop("_applied_tdp", None)
            state["active_app_id"] = cls._active_app
            state["tdp"] = dict(state.get("tdp", power_backend.DEFAULT_TDP))
            state["controller"] = dict(state.get("controller", controller_backend.DEFAULT_CONTROLLER))
            for side in ("left", "right"):
                state[f"module_{side}"] = dict(state.get(
                    f"module_{side}", controller_backend.module_info(side, status="detecting")))
        state["settings_error"] = getattr(settings, "recovery_error", "")
        return state

    @classmethod
    def _snapshot(cls, probe_tdp=True):
        with _lock:
            state = cls._copy_state()
            identity = cls._state
            tdp_epoch = cls._tdp_observation_epoch
        missing = object()
        before = {key: state.get(key, missing) for key in SNAPSHOT_DIAGNOSTICS}
        if state["settings_error"] or state.get("initializing") or state.get("startup_error"):
            return state
        if not state.get("supported", False):
            # Never probe AYANEO-specific EC or HID registers merely because a
            # privileged Decky RPC was called on another machine.
            state["gpu_power_w"] = None
            state["tdp_supported"] = False
            state["tdp_error"] = "This device is not an AYANEO 3"
            state["controller_supported"] = False
            state["controller_error"] = "This device is not an AYANEO 3"
            state["cpu_boost_supported"] = False
            state["cpu_boost"] = False
            state["charge_bypass_supported"] = False
            state["module_eject_supported"] = False
            state["module_reset_supported"] = False
            return state
        # Hardware reads can involve sysfs, debugfs and subprocesses. Keep them
        # outside the shared state lock so the monitor loops and RPC writes do
        # not stall behind the QAM's periodic refresh.
        state["gpu_power_w"] = power_backend.gpu_power_watts()
        if probe_tdp:
            state.update(power_backend.tdp_capabilities())
        try:
            controller_backend.vendor_hidraw()  # Discovery only; never issue a vendor command here.
            state["controller_supported"] = True
            state["controller_error"] = ""
        except (OSError, RuntimeError) as error:
            state["controller_supported"] = False
            state["controller_error"] = str(error)
            state["module_left"] = controller_backend.module_info("left", status="unavailable")
            state["module_right"] = controller_backend.module_info("right", status="unavailable")
        try:
            state["cpu_boost"] = power_backend.read_cpu_boost()
            state["cpu_boost_supported"] = True
        except RuntimeError:
            state["cpu_boost_supported"] = False
        state["screen_installed"] = display_backend.is_our_display_script(display_backend.LUA_TARGET)
        state["screen_conflict"] = display_backend.display_script_conflict(display_backend.LUA_TARGET)
        try:
            edid = display_backend.read_published_edid()
            state["edid_game_nits"] = round(display_backend.published_edid_nits(edid) or 0)
        except OSError:
            state["edid_game_nits"] = 0
        state["edid_patched"] = state["edid_game_nits"] == display_backend.EDID_TARGET_NITS
        state["button_fix_installed"] = input_backend.button_fix_installed()
        try:
            state["charge_bypass"] = ec_backend.read_charge_bypass()
            state["charge_bypass_supported"] = True
        except (OSError, RuntimeError):
            state["charge_bypass_supported"] = False
        try:
            presence = ec_backend.module_presence()
            state["modules_connected"] = all(presence.values())
            state["module_eject_supported"] = state["controller_supported"]
            state["module_reset_supported"] = state["controller_supported"]
            if not state["modules_connected"]:
                state["module_left"], state["module_right"] = \
                    controller_backend.module_states_from_presence(presence)
        except (OSError, RuntimeError):
            state["module_eject_supported"] = False
            state["module_reset_supported"] = False
        with _lock:
            if cls._state is not identity:
                return state
            for key in SNAPSHOT_DIAGNOSTICS:
                # A slow read must not undo newer capability information from
                # a transaction or monitor. Never publish its copied settings.
                if key in TDP_DIAGNOSTIC_FIELDS:
                    continue
                if key in state and cls._state.get(key, missing) == before[key]:
                    cls._state[key] = state[key]
            if (probe_tdp and not cls._tdp_writes and tdp_epoch == cls._tdp_observation_epoch
                    and all(cls._state.get(key, missing) == before[key]
                            for key in TDP_DIAGNOSTIC_FIELDS)):
                cls._state.update({key: state[key] for key in TDP_DIAGNOSTIC_FIELDS})
        return state

    def _schedule_tdp_diagnostic(self):
        # This cache is only for UI status. Every TDP write still checks live
        # ownership before touching RyzenAdj, including after a download.
        if self._closing or time.monotonic() < self._tdp_diagnostic_after:
            return
        if self._tdp_diagnostic_task is not None and not self._tdp_diagnostic_task.done():
            return
        with _lock:
            state = Plugin._state
            if (Plugin._tdp_writes or not state.get("supported") or state.get("initializing")
                    or state.get("startup_error") or getattr(settings, "recovery_error", "")):
                return
            epoch = Plugin._tdp_observation_epoch
            before = {key: state.get(key) for key in TDP_DIAGNOSTIC_FIELDS}

        async def observe():
            try:
                observed = await self._offload(power_backend.tdp_capabilities)
                with _lock:
                    if (not self._closing and not Plugin._tdp_writes and Plugin._state is state
                            and Plugin._tdp_observation_epoch == epoch
                            and all(state.get(key) == before[key] for key in TDP_DIAGNOSTIC_FIELDS)):
                        state.update(observed)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} TDP diagnostic failed: {error}")
            finally:
                self._tdp_diagnostic_after = time.monotonic() + TDP_DIAGNOSTIC_INTERVAL

        self._tdp_diagnostic_task = asyncio.create_task(observe())

    async def get_state(self):
        self._schedule_tdp_diagnostic()
        return await self._offload(self._snapshot, False)

    async def get_battery_status(self):
        return await self._offload(ec_backend.battery_status)

    async def get_fan_status(self):
        if not await self._offload(companion_common.supported_device):
            return {"supported": False, "mode": "auto", "percent": None,
                    "rpm": None, "reason": "This device is not an AYANEO 3"}
        return await self._offload(fan_controller.status)

    async def get_version(self):
        return {"version": companion_common.updater.plugin_version()}

    async def check_for_updates(self):
        return await self._offload(companion_common.updater.check)

    async def perform_update(self, expected_version):
        if expected_version is None:
            raise ValueError("Select a release version before downloading")
        return await self._offload(companion_common.updater.download, expected_version)

    async def set_tdp(self, raw, preset=None, expected_app_id=None):
        value = power_backend.normalize_tdp(raw)
        profile_name = power_backend.tdp_preset(value, preset)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                previous = dict(Plugin._state.get("_applied_tdp", Plugin._state["tdp"]))
                self._apply_and_save(
                    lambda: self._apply_tdp_target(value),
                    self._tdp_rollback(previous),
                    {"tdp": value, "tdp_preset": profile_name})
                with _lock:
                    Plugin._state["tdp"] = value
                    Plugin._state["tdp_preset"] = profile_name
                    Plugin._state.update(tdp_profile_pending=False, tdp_profile_error="",
                                         _applied_tdp=dict(value))

        await self._offload(mutate)
        return self._copy_state()

    async def set_cpu_boost(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with _tdp_mutation_lock:
                previous = power_backend.read_cpu_boost()
                self._apply_and_save(lambda: power_backend.write_cpu_boost(value),
                    lambda: power_backend.write_cpu_boost(previous), {"cpu_boost": value})
                with _lock:
                    Plugin._state["cpu_boost"] = value
                    Plugin._state["cpu_boost_supported"] = True
        await self._offload(mutate)
        return self._copy_state()

    async def get_game_profile(self, app_id):
        app_id = self._app_id(app_id)
        def read():
            # A settings commit holds its own lock across disk I/O. Waiting
            # for a consistent profile belongs in a worker, never in asyncio.
            profiles = settings.getSetting("game_profiles", {})
            value = profiles.get(app_id) if isinstance(profiles, dict) else None
            return {"exists": isinstance(value, dict),
                    "profile": power_backend.normalize_tdp(value) if isinstance(value, dict) else {},
                    "preset": power_backend.tdp_preset(value, value.get("preset"))
                    if isinstance(value, dict) else ""}
        return await self._offload(read)

    async def set_game_profile(self, app_id, raw, preset=None, expected_app_id=None):
        app_id = self._app_id(app_id)
        value = power_backend.normalize_tdp(raw)
        profile_name = power_backend.tdp_preset(value, preset)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                with _lock:
                    if not app_id or app_id != Plugin._active_app:
                        raise RuntimeError("game is no longer active")
                    previous = dict(Plugin._state.get("_applied_tdp", Plugin._state["tdp"]))
                    profiles = settings.getSetting("game_profiles", {})
                    profiles = dict(profiles) if isinstance(profiles, dict) else {}
                    if app_id not in profiles and len(profiles) >= 512:
                        raise RuntimeError("Maximum number of game profiles reached")
                    profiles[app_id] = {**value, "preset": profile_name}
                self._apply_and_save(lambda: self._apply_tdp_target(value),
                    self._tdp_rollback(previous), {"game_profiles": profiles})
                with _lock:
                    Plugin._state["tdp"] = value
                    Plugin._state["tdp_preset"] = profile_name
                    Plugin._state.update(tdp_profile_pending=False, tdp_profile_error="",
                                         _applied_tdp=dict(value))

        await self._offload(mutate)
        return self._copy_state()

    async def delete_game_profile(self, app_id, expected_app_id=None):
        app_id = self._app_id(app_id)

        def mutate():
            with _tdp_mutation_lock:
                self._check_game(expected_app_id)
                with _lock:
                    profiles = settings.getSetting("game_profiles", {})
                    profiles = dict(profiles) if isinstance(profiles, dict) else {}
                    profiles.pop(app_id, None)
                    target = power_backend.normalize_tdp(settings.getSetting("tdp", power_backend.DEFAULT_TDP))
                    profile_name = power_backend.tdp_preset(
                        target, settings.getSetting("tdp_preset", None))
                    active = bool(app_id and app_id == Plugin._active_app)
                    previous = dict(Plugin._state.get("_applied_tdp", Plugin._state["tdp"]))
                if active:
                    self._apply_and_save(lambda: self._apply_tdp_target(target),
                        self._tdp_rollback(previous), {"game_profiles": profiles})
                else:
                    self._save("game_profiles", profiles)
                with _lock:
                    if active:
                        Plugin._state["tdp"] = target
                        Plugin._state["tdp_preset"] = profile_name
                        Plugin._state.update(tdp_profile_pending=False, tdp_profile_error="",
                                             _applied_tdp=dict(target))

        await self._offload(mutate)
        return self._copy_state()

    async def set_active_app(self, app_id):
        app_id = self._app_id(app_id)
        if not companion_common.supported_device():
            with _lock:
                Plugin._active_app = app_id
            return

        with _lock:
            # Do not reserve the public queue behind the monitor's slow TDP
            # transaction when this heartbeat has no work to perform.
            if app_id == Plugin._active_app:
                return

        def mutate():
            with _tdp_mutation_lock:
                with _lock:
                    # The power monitor owns retries of a pending target. A
                    # six-second frontend heartbeat must not start a second
                    # slow ownership probe while holding the public RPC queue.
                    if app_id == Plugin._active_app:
                        return None
                    profiles = settings.getSetting("game_profiles", {})
                    profile = (profiles.get(app_id)
                               if app_id and isinstance(profiles, dict) else None)
                    target = power_backend.normalize_tdp(
                        profile if isinstance(profile, dict)
                        else settings.getSetting("tdp", power_backend.DEFAULT_TDP))
                    profile_name = power_backend.tdp_preset(
                        target,
                        profile.get("preset") if isinstance(profile, dict)
                        else settings.getSetting("tdp_preset", None))
                    # App tracking is independent of SMU/PowerStation availability.
                    # A snapshot must never pair a new game ID with the old profile.
                    previous = dict(Plugin._state.get("_applied_tdp", Plugin._state["tdp"]))
                    Plugin._state["_applied_tdp"] = previous
                    Plugin._active_app = app_id
                    Plugin._state.update(tdp=target, tdp_preset=profile_name,
                                         tdp_profile_pending=True, tdp_profile_error="")
                try:
                    self._apply_tracked_tdp(target)
                except power_backend.TdpUnavailableError:
                    return None
                return isinstance(profile, dict)

        used_profile = await self._offload(mutate)
        if used_profile is not None:
            decky.logger.info(
                f"{LOG} applied {'game ' + app_id if used_profile else 'global'} TDP")

    @classmethod
    def _controller_save_values(cls, previous, value):
        """Save a controller change; the first one also records the state found on the device."""
        values = {"controller": value}
        with _lock:
            first = not cls._state.get("controller_owned", True)
            seeded = cls._state.get("controller_seeded", False)
        if first and seeded:
            # Uninstall restores this, not Companion's defaults.
            values["controller_baseline"] = previous
        return values

    def _set_controller_transaction(self, patch, feedback=False):
        with controller_backend.controller_apply_lock:
            with _lock:
                previous = dict(Plugin._state["controller"])
            # Merge only after earlier writes and monitor recovery finish. A
            # remounted panel may still be displaying their older snapshot.
            value = controller_backend.normalize_controller({**previous, **patch})
            gain_changed = value["ff_gain"] != previous["ff_gain"]

            def apply():
                if feedback:
                    controller_backend.apply_controller(value, True, previous["vibration"], False)
                else:
                    controller_backend.apply_controller(value)
                if gain_changed:
                    controller_backend.set_vibration_gain(value["ff_gain"])

            def restore():
                # Always attempt both devices even if firmware recovery fails.
                failures = []
                try:
                    controller_backend.apply_controller(previous)
                except Exception as error:
                    failures.append(f"firmware: {error}")
                if gain_changed:
                    try:
                        controller_backend.set_vibration_gain(previous["ff_gain"])
                    except Exception as error:
                        failures.append(f"FF Gain: {error}")
                if failures:
                    raise RuntimeError("; ".join(failures))

            self._apply_and_save(apply, restore, Plugin._controller_save_values(previous, value))
            with _lock:
                Plugin._state.update(controller=value, controller_owned=True)

    async def set_controller(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("Expected controller settings")
        await self._offload(self._set_controller_transaction, dict(raw))
        return self._copy_state()

    async def set_controller_with_vibration_feedback(self, raw):
        if not isinstance(raw, dict):
            raise ValueError("Expected controller settings")
        await self._offload(self._set_controller_transaction, dict(raw), True)
        return self._copy_state()

    async def set_vibration_gain(self, percent):
        value = companion_common.clamp(percent, 0, 100)

        def mutate():
            with controller_backend.controller_apply_lock:
                with _lock:
                    previous = dict(Plugin._state["controller"])
                    controller = {**previous, "ff_gain": value}
                self._apply_and_save(lambda: controller_backend.set_vibration_gain(value),
                    lambda: controller_backend.set_vibration_gain(previous["ff_gain"]),
                    Plugin._controller_save_values(previous, controller))
                with _lock:
                    Plugin._state.update(controller=controller, controller_owned=True)

        await self._offload(mutate)
        return self._copy_state()

    async def test_vibration(self, duration_ms=VIBRATION_TEST_MS):
        with _lock:
            level = Plugin._state["controller"]["vibration"]
        try:
            await self._offload(controller_backend.play_vibration_test, level, duration_ms)
            return {"success": True}
        except Exception as error:
            decky.logger.error(f"{LOG} vibration test failed: {error}")
            return {"success": False, "error": str(error)}

    async def set_charge_bypass(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with _charge_mutation_lock:
                previous = ec_backend.read_charge_bypass()
                self._apply_and_save(lambda: ec_backend.write_charge_bypass(value),
                    lambda: ec_backend.write_charge_bypass(previous), {"charge_bypass": value})
                with _lock:
                    Plugin._state["charge_bypass"] = value
                    Plugin._state["charge_bypass_supported"] = True
        await self._offload(mutate)
        return self._copy_state()

    async def set_fan(self, mode, percent=None):
        def mutate():
            companion_common.require_supported_device("fan control")
            return fan_controller.set(mode, percent)
        result = await self._offload(mutate)
        if mode == "manual" and not self._closing:
            self._fan_generation += 1
            if Plugin._fan_task is None or Plugin._fan_task.done():
                Plugin._fan_task = asyncio.create_task(self._fan_loop())
        return result

    async def set_audio_fix(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with self._audio_mutation_lock:
                previous = bool(Plugin._state.get("audio_fix_enabled", False))
                def apply():
                    (audio_backend.apply_audio_fix if value else audio_backend.remove_audio_fix)()
                    if value and not audio_backend.audio_fix_ready():
                        raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
                try:
                    self._apply_and_save(apply,
                        audio_backend.apply_audio_fix if previous else audio_backend.remove_audio_fix,
                        {"audio_fix_enabled": value})
                except Exception as error:
                    installed = audio_backend.audio_fix_installed()
                    ready = audio_backend.audio_fix_ready() if installed else False
                    with _lock:
                        Plugin._state.update(audio_fix_enabled=previous,
                            audio_fix_error=str(error), audio_fix_installed=installed,
                            audio_calibration_available=previous and ready,
                            audio_profile="AYANEO v0.65" if ready else
                                "Installed, not active" if installed else "Generic fallback")
                    raise
                with _lock:
                    Plugin._state.update(audio_fix_enabled=value, audio_fix_error="",
                        audio_fix_installed=audio_backend.audio_fix_installed(),
                        audio_calibration_available=value,
                        audio_profile="AYANEO v0.65" if value else "Generic fallback")
        await self._offload(mutate)
        return self._copy_state()

    async def reapply_audio_fix(self):
        with _lock:
            enabled = Plugin._state.get("audio_fix_enabled", False)
        if not enabled:
            raise RuntimeError("enable the AYANEO audio tuning first")
        return await self.set_audio_fix(True)

    async def recalibrate_audio(self):
        with _lock:
            available = bool(Plugin._state.get("audio_calibration_available", False))
        if not available or not await self._offload(audio_backend.audio_fix_ready):
            raise RuntimeError("apply the AYANEO audio fix successfully before recalibrating")
        try:
            result = await self._offload(audio_backend.perform_audio_recalibration)
            summary = (
                f"{result['timestamp']} · L {result['left']} · R {result['right']} · "
                f"{result['ambient']} °C · Restart required")
            decky.logger.info(
                f"{LOG} saved speaker calibration L={result['left']} R={result['right']} "
                f"backup={result['backup']}")
            try:
                await self._offload(self._save, "audio_calibration_last", summary)
            except Exception as error:
                raise RuntimeError(
                    f"Speaker calibration was applied, but its summary could not be saved: {error}. "
                    f"Previous calibration backup: {result['backup']}") from error
            with _lock:
                Plugin._state["audio_calibration_available"] = True
                Plugin._state["audio_calibration_last"] = summary
                Plugin._state["audio_fix_error"] = ""
        except Exception as error:
            ready = await self._offload(audio_backend.audio_fix_ready)
            with _lock:
                Plugin._state["audio_calibration_available"] = ready
                Plugin._state["audio_fix_error"] = str(error)
            raise
        return self._copy_state()

    async def eject_modules(self, side):
        side = str(side or "").lower()
        with _lock:
            if Plugin._state.get("modules_reconnecting"):
                raise RuntimeError("reinsert both modules before ejecting again")
            controller = dict(Plugin._state["controller"])
            Plugin._state["modules_reconnecting"] = True
            Plugin._state["modules_connected"] = False
            Plugin._state["modules_detached"] = False
            if side in ("left", "both"):
                Plugin._state["module_left"] = controller_backend.module_info("left", status="ejecting")
            if side in ("right", "both"):
                Plugin._state["module_right"] = controller_backend.module_info("right", status="ejecting")
        try:
            await self._offload(controller_backend.eject_controller_modules, side, controller)
        except Exception:
            try:
                presence = await self._offload(ec_backend.module_presence)
                left, right = controller_backend.module_states_from_presence(presence)
                connected = all(presence.values())
            except Exception:
                left = controller_backend.module_info("left", status="unavailable")
                right = controller_backend.module_info("right", status="unavailable")
                connected = False
            with _lock:
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_connected"] = connected
                Plugin._state["modules_detached"] = False
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
            raise
        decky.logger.info(f"{LOG} ejected {side} controller module(s)")
        return self._copy_state()

    async def reset_modules(self):
        with _lock:
            if Plugin._state.get("modules_reconnecting"):
                raise RuntimeError("insert both modules before resetting")
            controller = dict(Plugin._state["controller"])
            restore_buttons = Plugin._state.get("button_fix_installed", False)
            Plugin._state["modules_reconnecting"] = True
            Plugin._state["modules_detached"] = False
            Plugin._state["module_left"] = controller_backend.module_info("left", status="activating")
            Plugin._state["module_right"] = controller_backend.module_info("right", status="activating")
        try:
            await self._offload(controller_backend.reset_controller_modules, controller, restore_buttons)
            left, right = await self._offload(controller_backend.read_module_layout)
            with _lock:
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
                Plugin._state["modules_connected"] = True
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_detached"] = False
            decky.logger.info(f"{LOG} reset and re-detected both Magic Modules")
        except Exception:
            left = controller_backend.module_info("left", status="unavailable")
            right = controller_backend.module_info("right", status="unavailable")
            connected = False
            try:
                presence = await self._offload(ec_backend.module_presence)
                left, right = controller_backend.module_states_from_presence(presence)
                connected = all(presence.values())
            except Exception:
                pass
            with _lock:
                Plugin._state["modules_reconnecting"] = False
                Plugin._state["modules_connected"] = connected
                Plugin._state["modules_detached"] = False
                Plugin._state["module_left"] = left
                Plugin._state["module_right"] = right
            raise
        return self._copy_state()

    async def set_screen_fix(self, enabled, replace_existing=False):
        enabled = self._boolean(enabled)
        replace_existing = self._boolean(replace_existing)
        def mutate():
            if display_backend.LUA_TARGET.is_symlink():
                raise RuntimeError("refusing a symlinked gamescope definition")
            previous = display_backend.LUA_TARGET.read_bytes() if display_backend.LUA_TARGET.exists() else None
            backup = None
            def apply():
                nonlocal backup
                if enabled:
                    backup = display_backend.install_display_script(replace_existing)
                else:
                    display_backend.remove_display_script()
            def restore():
                if display_backend.LUA_TARGET.is_symlink():
                    raise RuntimeError("display definition changed to a symlink")
                target = display_backend.LUA_TARGET
                current = target.read_bytes() if target.exists() else None
                if current == previous:
                    return
                if current is not None and not display_backend.display_script_owned(display_backend.LUA_TARGET):
                    raise RuntimeError("display definition changed outside Companion")
                if previous is None:
                    display_backend.LUA_TARGET.unlink(missing_ok=True)
                else:
                    companion_common.atomic_write_bytes(display_backend.LUA_TARGET, previous)
            self._apply_and_save(apply, restore, {"screen_fix_enabled": enabled})
            conflict = display_backend.display_script_conflict(display_backend.LUA_TARGET)
            with _lock:
                Plugin._state["screen_installed"] = enabled
                Plugin._state["screen_conflict"] = conflict
            return backup
        try:
            backup = await self._offload(mutate)
            if enabled:
                if backup is not None:
                    decky.logger.info(
                        f"{LOG} replaced existing display definition; backup={backup}")
                try:
                    await self._offload(display_backend.patch_published_edid)
                except Exception as error:
                    # The Lua definition is already installed. The EDID file is
                    # recreated by gamescope and the monitor loop will retry it,
                    # so a transient race here must not roll back the UI switch.
                    decky.logger.warning(f"{LOG} initial EDID normalization deferred: {error}")
        except Exception as error:
            decky.logger.error(f"{LOG} display definition change failed: {error}")
            raise
        return self._copy_state()

    async def _edid_loop(self):
        """Reapply after every gamescope session recreates its EDID copy."""
        while True:
            try:
                if display_backend.is_our_display_script(display_backend.LUA_TARGET):
                    await self._offload(display_backend.patch_published_edid)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} EDID verification failed: {error}")
            await asyncio.sleep(1)

    async def set_button_fix(self, enabled):
        value = self._boolean(enabled)
        def mutate():
            with controller_backend.controller_apply_lock:
                controller = dict(Plugin._state["controller"])
                paths = (input_backend.INPUT_MAP_TARGET, input_backend.LEGACY_INPUT_DEVICE_TARGET,
                         *input_backend.LEGACY_INPUT_MAP_TARGETS)
                before = {}
                for path in paths:
                    if path.is_symlink():
                        raise RuntimeError("refusing a symlinked InputPlumber override")
                    if path.exists():
                        if (path == input_backend.INPUT_MAP_TARGET and value
                                and not input_backend.button_map_owned(path)):
                            raise RuntimeError("another aya7 InputPlumber override already exists")
                        if input_backend.button_map_owned(path):
                            before[path] = path.read_bytes()
                previous_enabled = bool(before)
                try:
                    controller_backend.program_rear_buttons(value, controller if value else None)
                    (input_backend.install_button_fix if value else input_backend.remove_button_fix)()
                    _systemctl("restart", "inputplumber", check=True)
                    self._save("button_fix_enabled", value)
                except Exception as error:
                    failures = []
                    try:
                        controller_backend.program_rear_buttons(
                            previous_enabled, controller if previous_enabled else None)
                    except Exception as recovery:
                        failures.append(f"controller: {recovery}")
                    for path in paths:
                        try:
                            if path in before:
                                if path.exists() and not input_backend.button_map_owned(path):
                                    raise RuntimeError("the override changed outside Companion")
                                companion_common.atomic_write_bytes(path, before[path])
                            elif input_backend.button_map_owned(path):
                                path.unlink()
                        except Exception as recovery:
                            failures.append(f"map: {recovery}")
                    try:
                        _systemctl("restart", "inputplumber", check=True)
                    except Exception as recovery:
                        failures.append(f"InputPlumber: {recovery}")
                    detail = "; rollback failed: " + "; ".join(failures) if failures else "; previous bindings restored"
                    raise RuntimeError(f"{error}{detail}") from error
                with _lock:
                    Plugin._state["button_fix_installed"] = value
        await self._offload(mutate)
        return self._copy_state()

    async def set_tm_guard(self, enabled):
        value = self._boolean(enabled)
        def commit():
            with controller_backend.controller_apply_lock:
                self._save("tm_guard_enabled", value)
                with _lock:
                    Plugin._state["tm_guard_enabled"] = value
                    Plugin._state["tm_guard_status"] = "Monitoring" if value else "Disabled"
        await self._offload(commit)
        if value:
            with _lock:
                controller = dict(Plugin._state["controller"])
                restore_buttons = Plugin._state.get("button_fix_installed", False)
                owned = Plugin._state.get("controller_owned", True)
            try:
                changed = await self._offload(controller_backend.recover_tm_mode, controller,
                                              restore_buttons, owned)
            except Exception as error:
                with _lock:
                    Plugin._state["tm_guard_status"] = "Waiting for controller"
                decky.logger.debug(f"{LOG} TM Guard initial check pending: {error}")
            else:
                if changed:
                    with _lock:
                        Plugin._state["tm_guard_recoveries"] += 1
                        Plugin._state["tm_guard_status"] = "Custom mode restored"
        return self._copy_state()

    async def _restore_hardware(self, rear_buttons=False):
        """Retry pending boot work; each attempt reads the latest saved choices."""
        pending = {"tdp", "controller"}
        if rear_buttons:
            pending.add("rear_buttons")
        for key in ("charge_bypass", "cpu_boost"):
            if type(settings.getSetting(key, None)) is bool:
                pending.add(key)
        attempt = 0
        while pending:
            await asyncio.sleep((1, 2, 4, 8, 30, 60)[min(attempt, 5)])
            attempt += 1
            for key in tuple(sorted(pending)):
                try:
                    if key == "tdp":
                        await self._offload(Plugin._reapply_current_tdp)
                    elif key == "controller":
                        await self._offload(Plugin._restore_current_controller)
                    elif key == "rear_buttons":
                        await self._offload(Plugin._restore_rear_buttons)
                    else:
                        await self._offload(self._restore_saved_control, key)
                    pending.remove(key)
                    decky.logger.info(f"{LOG} restored {key} after startup")
                except asyncio.CancelledError:
                    raise
                except power_backend.TdpUnavailableError as error:
                    pending.remove(key)
                    decky.logger.info(f"{LOG} {error}")
                except Exception as error:
                    decky.logger.warning(f"{LOG} {key} restore attempt failed: {error}")

    def _restore_saved_control(self, key, only_if_changed=False):
        # CPU Boost is reapplied by TDP writes; charge control is independent.
        mutation_lock = _tdp_mutation_lock if key == "cpu_boost" else _charge_mutation_lock
        with mutation_lock:
            value = settings.getSetting(key, None)
            if type(value) is not bool:
                return
            read, write = ((power_backend.read_cpu_boost, power_backend.write_cpu_boost) if key == "cpu_boost"
                           else (ec_backend.read_charge_bypass, ec_backend.write_charge_bypass))
            if not only_if_changed or read() != value:
                write(value)
            with _lock:
                Plugin._state[key] = value
                Plugin._state[key + "_supported"] = True

    async def _restore_audio(self):
        """Retry late ALSA discovery without applying an obsolete enabled choice."""
        attempt = 0
        while True:
            await asyncio.sleep((1, 2, 4, 8, 30, 60)[min(attempt, 5)])
            attempt += 1
            try:
                if await self._offload(self._restore_audio_once):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                with _lock:
                    Plugin._state["audio_calibration_available"] = False
                    Plugin._state["audio_fix_error"] = str(error)
                decky.logger.warning(f"{LOG} audio tuning attempt failed: {error}")

    def _restore_audio_once(self):
        with self._audio_mutation_lock:
            with _lock:
                state = Plugin._state
                enabled = state.get("audio_fix_enabled", False)
                if self._closing:
                    return True
            if not enabled:
                # Discovery also runs for a saved disabled choice, without
                # touching DSPs. Late ALSA availability must allow enabling
                # tuning without restarting the plugin.
                supported = audio_backend.audio_fix_supported()
                installed = audio_backend.audio_fix_installed()
                with _lock:
                    if not self._closing and Plugin._state is state:
                        state.update(audio_fix_supported=supported,
                            audio_fix_installed=installed, audio_calibration_available=False,
                            audio_profile="Installed, not verified" if installed else "Generic fallback",
                            audio_fix_error="")
                return supported
            if not audio_backend.audio_fix_ready():
                supported = audio_backend.audio_fix_supported()
                with _lock:
                    if not self._closing and Plugin._state is state:
                        state["audio_fix_supported"] = supported
                if not supported:
                    return False
                if self._closing:
                    return True
                audio_backend.apply_audio_fix()
                if not audio_backend.audio_fix_ready():
                    raise RuntimeError("both AYANEO speaker DSPs did not enter the tuned profile")
            with _lock:
                if not self._closing and Plugin._state is state:
                    state.update(audio_fix_supported=True, audio_fix_installed=True,
                        audio_calibration_available=True, audio_profile="AYANEO v0.65",
                        audio_fix_error="")
            return True

    async def _ac_loop(self):
        """Restore deferred profiles, charger/resume limits and CPU drift."""
        previous = None
        suspend_offset = _suspend_clock_offset()
        next_control_check = 0.0
        next_profile_retry = time.monotonic() + 5.0
        pending_delays = []
        settle_at = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                current = await self._offload(ec_backend.ac_online)
                offset = _suspend_clock_offset()
                resumed = (offset is not None and suspend_offset is not None
                           and offset - suspend_offset > CONTROLLER_RESUME_THRESHOLD)
                if offset is not None:
                    suspend_offset = offset
                if (previous is not None and current != previous) or resumed:
                    pending_delays = [0.5, 1.0, 1.5, 3.0]
                    settle_at = time.monotonic() + pending_delays.pop(0)
                    next_control_check = 0.0
                previous = current
                now = time.monotonic()
                tdp_attempted = False
                if settle_at and now >= settle_at:
                    # Advance only after success; transient failures are retried.
                    tdp_attempted = True
                    try:
                        await self._offload(Plugin._reapply_current_tdp)
                    except power_backend.TdpUnavailableError as error:
                        # The profile remains pending. A separate retry handles
                        # ownership changes without requiring another AC event.
                        settle_at = 0.0
                        pending_delays = []
                        decky.logger.debug(f"{LOG} TDP restoration deferred: {error}")
                    except Exception as error:
                        settle_at = now + 5.0
                        decky.logger.warning(f"{LOG} TDP restoration retrying: {error}")
                    else:
                        settle_at = now + pending_delays.pop(0) if pending_delays else 0.0
                    next_profile_retry = now + 5.0
                with _lock:
                    profile_pending = Plugin._state.get("tdp_profile_pending", False)
                if profile_pending and not tdp_attempted and now >= next_profile_retry:
                    next_profile_retry = now + 5.0
                    try:
                        await self._offload(Plugin._reapply_current_tdp)
                    except power_backend.TdpUnavailableError as error:
                        decky.logger.debug(f"{LOG} game profile waiting for TDP control: {error}")
                    except Exception as error:
                        decky.logger.warning(f"{LOG} game profile restoration retrying: {error}")
                if now >= next_control_check:
                    next_control_check = now + 15.0
                    try:
                        await self._offload(self._restore_saved_control, "cpu_boost", True)
                    except Exception as error:
                        decky.logger.debug(f"{LOG} cpu_boost verification deferred: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.warning(f"{LOG} power monitor retrying: {error}")

    async def _charge_loop(self):
        """Verify charging independently of slow TDP retries and downloads."""
        previous = None
        suspend_offset = _suspend_clock_offset()
        next_check = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                current = await self._offload(ec_backend.ac_online)
                offset = _suspend_clock_offset()
                resumed = (offset is not None and suspend_offset is not None
                           and offset - suspend_offset > CONTROLLER_RESUME_THRESHOLD)
                if offset is not None:
                    suspend_offset = offset
                changed = previous is not None and current != previous
                previous = current
                now = time.monotonic()
                if now < next_check and not changed and not resumed:
                    continue
                next_check = now + 15.0
                await self._offload(self._restore_saved_control, "charge_bypass", True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                decky.logger.debug(f"{LOG} charge_bypass verification deferred: {error}")

    async def _fan_loop(self):
        """Renew an independent fan safety timer only while Decky stays alive."""
        suspend_offset = _suspend_clock_offset()
        while True:
            await asyncio.sleep(FAN_RENEW_SECONDS)
            generation = self._fan_generation
            try:
                offset = _suspend_clock_offset()
                resumed = (offset is not None and suspend_offset is not None
                           and offset - suspend_offset > CONTROLLER_RESUME_THRESHOLD)
                if offset is not None:
                    suspend_offset = offset
                if resumed:
                    await self._offload(fan_controller.close)
                    if generation != self._fan_generation:
                        continue
                    decky.logger.info(f"{LOG} restored automatic fan mode after resume")
                    return
                if not await self._offload(fan_controller.renew):
                    # A completed worker can report the old lease after a new
                    # manual RPC has succeeded. Keep that new lease monitored.
                    if generation == self._fan_generation:
                        return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if generation != self._fan_generation:
                    continue
                decky.logger.warning(f"{LOG} manual fan mode ended: {error}")
                return

    async def _module_loop(self):
        """Track module identity and restore the controller after a module swap."""
        previous_connected = None
        restore_pending = False
        identify_pending = True
        retry_delay = 0.5
        identify_at = 0.0
        identify_delay = 0.5
        while True:
            await asyncio.sleep(retry_delay)
            try:
                # Cutting controller power also removes its vendor HID node.
                # Read the independent EC first so physical reinsertion can
                # restore power before requiring the USB interface to exist.
                powered = await self._offload(ec_backend.controller_powered)
                presence = await self._offload(ec_backend.module_presence)
                retry_delay = 0.5
                connected = all(presence.values())
                reconnected = previous_connected is False and connected
                previous_connected = connected
                with _lock:
                    Plugin._state["modules_connected"] = connected
                    reconnecting = Plugin._state.get("modules_reconnecting", False)
                    detached = Plugin._state.get("modules_detached", False)
                if not connected:
                    left, right = controller_backend.module_states_from_presence(presence)
                    with _lock:
                        if reconnecting:
                            Plugin._state["modules_detached"] = True
                        Plugin._state["module_left"] = left
                        Plugin._state["module_right"] = right
                    identify_pending = True
                    identify_at = 0.0
                    identify_delay = 0.5
                    continue
                # During eject, both EC presence bits can remain asserted until
                # the released module is physically lifted. Do not immediately
                # undo the deliberate controller power-off. Quick Reset also
                # owns the controller until its RPC clears this state.
                if reconnecting and not detached:
                    continue
                if not powered:
                    if not await self._offload(self._power_modules_if_allowed):
                        continue
                    restore_pending = True
                elif reconnected:
                    # A manually removed module loses its LED state while the
                    # base controller remains powered, so power state alone is
                    # not enough to detect that RGB needs to be sent again.
                    restore_pending = True
                try:
                    # This discovery still rejects a kernel-owned hid-ayaneo
                    # device before any raw controller transaction is attempted.
                    await self._offload(controller_backend.vendor_hidraw)
                except (OSError, RuntimeError) as error:
                    with _lock:
                        Plugin._state["controller_supported"] = False
                        Plugin._state["controller_error"] = str(error)
                        Plugin._state["module_left"] = controller_backend.module_info("left", status="unavailable")
                        Plugin._state["module_right"] = controller_backend.module_info("right", status="unavailable")
                    retry_delay = 2.0
                    identify_pending = True
                    continue
                with _lock:
                    Plugin._state["controller_supported"] = True
                    Plugin._state["controller_error"] = ""
                if restore_pending:
                    with _lock:
                        Plugin._state["module_left"] = controller_backend.module_info("left", status="activating")
                        Plugin._state["module_right"] = controller_backend.module_info("right", status="activating")
                last_error = None
                if restore_pending:
                    for delay in (0.5, 1.0, 2.0, 3.0):
                        await asyncio.sleep(delay)
                        try:
                            if not await self._offload(ec_backend.both_modules_connected):
                                break
                            # Rear-button mappings are persisted with AYA_SAVE.
                            # Reprogramming them here would also issue the 0x88
                            # physical module reset on every reconnection.
                            restored = await self._offload(Plugin._restore_current_controller)
                            with _lock:
                                if (Plugin._state.get("modules_reconnecting")
                                        and not Plugin._state.get("modules_detached")):
                                    break
                            restore_pending = False
                            identify_pending = True
                            with _lock:
                                Plugin._state["modules_reconnecting"] = False
                                Plugin._state["modules_detached"] = False
                            if restored is not None:
                                decky.logger.info(
                                    f"{LOG} modules connected; restored RGB, vibration and FF_GAIN")
                            break
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            last_error = error
                if restore_pending and last_error:
                    decky.logger.warning(f"{LOG} controller setting restore failed: {last_error}")
                if identify_pending and not restore_pending and time.monotonic() >= identify_at:
                    try:
                        left, right = await self._offload(controller_backend.read_module_layout)
                        with _lock:
                            Plugin._state["module_left"] = left
                            Plugin._state["module_right"] = right
                        identify_pending = False
                        identify_delay = 0.5
                        decky.logger.info(
                            f"{LOG} modules detected: left {left['label']} (0x{left['code']:02X}), "
                            f"right {right['label']} (0x{right['code']:02X})")
                    except Exception as error:
                        with _lock:
                            Plugin._state["module_left"] = controller_backend.module_info("left", status="unavailable")
                            Plugin._state["module_right"] = controller_backend.module_info(
                                "right", status="unavailable")
                            Plugin._state["controller_error"] = str(error)
                        identify_delay = min(30.0, identify_delay * 2)
                        identify_at = time.monotonic() + identify_delay
                        decky.logger.debug(f"{LOG} module identification pending: {error}")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                retry_delay = min(30.0, retry_delay * 2)
                decky.logger.debug(f"{LOG} module monitor unavailable: {error}")

    @staticmethod
    def _power_modules_if_allowed():
        with controller_backend.controller_apply_lock:
            with _lock:
                if (Plugin._state.get("modules_reconnecting")
                        and not Plugin._state.get("modules_detached")):
                    return False
            if not ec_backend.both_modules_connected():
                return False
            if not ec_backend.controller_powered():
                ec_backend.set_controller_power(True)
            return True

    async def _vibration_gain_loop(self):
        """Restore evdev gain independently of vendor HID availability."""
        last_token = None
        restore_pending = False
        last_suspend_offset = _suspend_clock_offset()
        while True:
            await asyncio.sleep(CONTROLLER_GAIN_CHECK_INTERVAL)
            suspend_offset = _suspend_clock_offset()
            if (suspend_offset is not None and last_suspend_offset is not None
                    and suspend_offset - last_suspend_offset > CONTROLLER_RESUME_THRESHOLD):
                restore_pending = True
            if suspend_offset is not None:
                last_suspend_offset = suspend_offset
            with _lock:
                ready = (Plugin._state.get("modules_connected", False)
                         and not Plugin._state.get("modules_reconnecting", False))
            if not ready:
                last_token = None
                continue
            try:
                token = await self._offload(controller_backend.rumble_event_token)
                if token is None:
                    last_token = None
                    continue
                if token != last_token or restore_pending:
                    # Recheck module intent and read the latest gain under the
                    # controller lock, after any earlier slider/reset operation.
                    restored = await self._offload(Plugin._restore_current_vibration_gain)
                    last_token = token
                    restore_pending = False
                    if restored is not None:
                        decky.logger.info(f"{LOG} restored FF_GAIN on the physical input device")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Keep a failed incarnation/resume pending for the next cycle.
                decky.logger.debug(f"{LOG} FF_GAIN restoration waiting: {error}")

    async def _tm_guard_loop(self):
        """Repair controller resets and optionally undo accidental TM changes."""
        was_available = None
        last_device_token = None
        last_suspend_offset = _suspend_clock_offset()
        retry_delay = TM_GUARD_INTERVAL
        while True:
            await asyncio.sleep(retry_delay)
            suspend_offset = _suspend_clock_offset()
            resumed = bool(
                suspend_offset is not None
                and last_suspend_offset is not None
                and suspend_offset - last_suspend_offset > CONTROLLER_RESUME_THRESHOLD
            )
            if suspend_offset is not None:
                last_suspend_offset = suspend_offset
            with _lock:
                enabled = Plugin._state.get("tm_guard_enabled", False)
                reconnecting = Plugin._state.get("modules_reconnecting", False)
                connected = Plugin._state.get("modules_connected", False)
            if reconnecting or not connected:
                was_available = None
                last_device_token = None
                with _lock:
                    Plugin._state["tm_guard_status"] = (
                        "Waiting for modules" if enabled else "Disabled")
                continue
            try:
                status, path = await self._offload(
                    Plugin._reconcile_current_controller,
                    enabled, was_available is False or resumed)
                device_token = await self._offload(controller_backend.device_node_token, path)
                if (last_device_token is not None
                        and device_token != last_device_token
                        and status == "healthy"):
                    status, path = await self._offload(
                        Plugin._reconcile_current_controller, enabled, True)
                    device_token = await self._offload(controller_backend.device_node_token, path)
                was_available = True
                retry_delay = TM_GUARD_INTERVAL
                last_device_token = device_token
                if resumed and status in ("mode_restored", "configuration_restored"):
                    decky.logger.info(
                        f"{LOG} restored controller configuration after system resume")
                with _lock:
                    if status == "mode_restored":
                        Plugin._state["tm_guard_recoveries"] += 1
                    Plugin._state["tm_guard_status"] = (
                        "Custom mode restored"
                        if enabled and status == "mode_restored"
                        else "Monitoring" if enabled else "Disabled")
                if status == "mode_restored":
                    decky.logger.info(f"{LOG} TM Guard restored custom controller mode")
                elif status == "configuration_restored":
                    decky.logger.info(
                        f"{LOG} restored drifted RGB, vibration and FF_GAIN")
                elif status == "tm_mode":
                    # TM Guard is intentionally disabled, so leave the user's
                    # selected hardware mode untouched.
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                was_available = False
                retry_delay = min(30.0, retry_delay * 2)
                with _lock:
                    Plugin._state["tm_guard_status"] = (
                        "Waiting for controller" if enabled else "Disabled")
                decky.logger.debug(f"{LOG} controller monitor waiting: {error}")

    async def _main(self):
        if self._cleanup_task is not None:
            await complete(self._cleanup_task)
            self._cleanup_task = None
        if self._startup_task is not None and not self._startup_task.done():
            return await complete(self._startup_task)
        self._closing = False
        self._initializing = True
        self._tdp_diagnostic_task = None
        self._tdp_diagnostic_after = 0.0
        companion_common.updater.reset()
        with _lock:
            Plugin._state = {"initializing": True}
        try:
            self._startup_task = asyncio.create_task(self._start())
            await complete(self._startup_task)
        except asyncio.CancelledError:
            await self._unload()
            raise
        except Exception as error:
            with _lock:
                Plugin._state["startup_error"] = str(error)
            decky.logger.error(f"{LOG} startup failed: {error}")
        finally:
            self._initializing = False
            with _lock:
                Plugin._state["initializing"] = False

    async def _start(self):
        await self._offload(settings.read)
        is_supported = await self._offload(companion_common.supported_device)
        storage_error = getattr(settings, "recovery_error", "")
        hardware_allowed = is_supported and not storage_error
        if is_supported:
            try:
                await self._offload(fan_controller.recover_abandoned)
            except Exception as error:
                decky.logger.warning(f"{LOG} previous fan session recovery pending: {error}")
        try:
            await self._offload(companion_common.updater.ssl_context)
        except Exception as error:
            decky.logger.warning(f"{LOG} updater TLS initialization failed: {error}")
        saved_controller = controller_backend.normalize_controller(
            settings.getSetting("controller", controller_backend.DEFAULT_CONTROLLER))
        # Older releases did not persist either switch. Migrate an existing
        # owned file into a saved preference, then restore it after OS updates
        # that replace /etc. A missing file with no preference stays disabled.
        existing_key_binding = any(input_backend.button_map_owned(path) for path in (
            input_backend.INPUT_MAP_TARGET, *input_backend.LEGACY_INPUT_MAP_TARGETS,
            input_backend.LEGACY_INPUT_DEVICE_TARGET))
        saved_button_fix = settings.getSetting("button_fix_enabled", None)
        desired_button_fix = (saved_button_fix if type(saved_button_fix) is bool
                              else existing_key_binding)
        reprogram_rear_buttons = False
        if hardware_allowed and desired_button_fix:
            try:
                if not await self._offload(input_backend.button_fix_installed) or any(
                        input_backend.button_map_owned(path) for path in (
                            *input_backend.LEGACY_INPUT_MAP_TARGETS, input_backend.LEGACY_INPUT_DEVICE_TARGET)):
                    await self._offload(input_backend.install_button_fix)
                    # Uninstall clears the LC1/RC1 firmware slots together with
                    # this map, so a reinstalled map also needs its firmware half.
                    reprogram_rear_buttons = True
                    await self._offload(_systemctl, "restart", "inputplumber", check=True)
            except Exception as error:
                decky.logger.warning(f"{LOG} key-binding restoration failed: {error}")
        elif hardware_allowed and saved_button_fix is False and existing_key_binding:
            try:
                await self._offload(input_backend.remove_button_fix)
                await self._offload(_systemctl, "restart", "inputplumber", check=True)
            except Exception as error:
                decky.logger.warning(f"{LOG} key-binding disable recovery failed: {error}")
        key_binding_installed = await self._offload(input_backend.button_fix_installed)
        existing_screen = display_backend.display_script_owned(display_backend.LUA_TARGET)
        saved_screen_fix = settings.getSetting("screen_fix_enabled", None)
        desired_screen_fix = (saved_screen_fix if type(saved_screen_fix) is bool
                              else existing_screen)
        if (hardware_allowed and desired_screen_fix
                and not display_backend.is_our_display_script(display_backend.LUA_TARGET)):
            try:
                await self._offload(display_backend.install_display_script)
            except Exception as error:
                decky.logger.warning(f"{LOG} display-definition restoration failed: {error}")
        elif hardware_allowed and saved_screen_fix is False and existing_screen:
            try:
                await self._offload(display_backend.remove_display_script)
            except Exception as error:
                decky.logger.warning(f"{LOG} display-definition disable recovery failed: {error}")
        screen_installed = display_backend.is_our_display_script(display_backend.LUA_TARGET)
        if hardware_allowed and not storage_error:
            migrations = {}
            if saved_button_fix is None and existing_key_binding:
                migrations["button_fix_enabled"] = True
            if saved_screen_fix is None and existing_screen:
                migrations["screen_fix_enabled"] = True
            if migrations:
                try:
                    await self._offload(self._save_values, migrations)
                except Exception as error:
                    decky.logger.warning(f"{LOG} legacy fix preference migration failed: {error}")
        audio_enabled = settings.getSetting("audio_fix_enabled", True) is not False
        tm_guard_enabled = settings.getSetting("tm_guard_enabled", True) is not False
        audio_installed = await self._offload(audio_backend.audio_fix_installed) if hardware_allowed else False
        ec_control = await self._offload(ec_backend.ensure_controller_control) if hardware_allowed else None
        charge_control = (await self._offload(ec_backend.ensure_charge_bypass_control)
                          if hardware_allowed else None)
        try:
            charge_bypass = await self._offload(ec_backend.read_charge_bypass) if charge_control else False
        except (OSError, RuntimeError):
            charge_control = None
            charge_bypass = False
        saved_tdp = power_backend.normalize_tdp(settings.getSetting("tdp", power_backend.DEFAULT_TDP))
        try:
            cpu_boost = await self._offload(power_backend.read_cpu_boost) if hardware_allowed else False
            cpu_boost_supported = is_supported
        except RuntimeError:
            cpu_boost = False
            cpu_boost_supported = False
        initial_state = {
            "supported": is_supported,
            "settings_error": storage_error,
            "initializing": False,
            "startup_error": "",
            "device": companion_common.dmi("product_name") or "unknown",
            "version": companion_common.updater.plugin_version(),
            "tdp_backend": "Checking" if hardware_allowed else "Unavailable",
            "tdp_supported": False,
            "tdp_error": "Checking TDP ownership" if hardware_allowed else "Hardware controls are unavailable",
            "tdp": saved_tdp,
            "tdp_preset": power_backend.tdp_preset(saved_tdp, settings.getSetting("tdp_preset", None)),
            "tdp_profile_pending": hardware_allowed,
            "tdp_profile_error": "",
            "presets": power_backend.PRESETS,
            "cpu_boost_supported": cpu_boost_supported,
            "cpu_boost": cpu_boost,
            "controller": saved_controller,
            # Without a saved choice the controller keeps its own lighting and
            # vibration; the monitors read them instead of applying defaults.
            "controller_owned": type(settings.getSetting("controller", None)) is dict,
            "controller_seeded": False,
            "screen_installed": screen_installed,
            "screen_conflict": display_backend.display_script_conflict(display_backend.LUA_TARGET),
            "edid_patched": False,
            "edid_game_nits": 0,
            "button_fix_installed": key_binding_installed,
            "charge_bypass_supported": charge_control is not None,
            "charge_bypass": charge_bypass,
            "module_eject_supported": ec_control is not None,
            "module_reset_supported": ec_control is not None,
            "modules_reconnecting": False,
            "modules_connected": True,
            "modules_detached": False,
            "module_left": controller_backend.module_info("left", status="detecting"),
            "module_right": controller_backend.module_info("right", status="detecting"),
            "tm_guard_enabled": tm_guard_enabled,
            "tm_guard_status": "Monitoring" if tm_guard_enabled else "Disabled",
            "tm_guard_recoveries": 0,
            "gpu_power_w": None,
            # ALSA commands have multi-second timeouts. Their background
            # discovery must not delay unrelated controls during startup.
            "audio_fix_supported": False,
            "audio_fix_enabled": audio_enabled,
            "audio_fix_installed": audio_installed,
            "audio_calibration_available": False,
            "audio_calibration_last": settings.getSetting("audio_calibration_last", ""),
            "audio_profile": "Pending" if audio_enabled else "Checking",
            "audio_fix_error": "",
        }
        with _lock:
            Plugin._state = initial_state
            Plugin._active_app = ""
        self._schedule_tdp_diagnostic()
        if hardware_allowed and not self._closing:
            Plugin._restore_task = asyncio.create_task(
                self._restore_hardware(reprogram_rear_buttons))
            Plugin._edid_task = asyncio.create_task(self._edid_loop())
            Plugin._ac_task = asyncio.create_task(self._ac_loop())
            Plugin._charge_task = asyncio.create_task(self._charge_loop())
            Plugin._module_task = asyncio.create_task(self._module_loop())
            Plugin._tm_guard_task = asyncio.create_task(self._tm_guard_loop())
            Plugin._vibration_gain_task = asyncio.create_task(self._vibration_gain_loop())
            Plugin._audio_task = asyncio.create_task(self._restore_audio())
        decky.logger.info(f"{LOG} started on {Plugin._state['device']}")

    async def _unload(self):
        if self._cleanup_task is not None:
            return await complete(self._cleanup_task)
        self._closing = True
        companion_common.updater.close()
        async def cleanup():
            if self._startup_task is not None and not self._startup_task.done():
                self._startup_task.cancel()
                await asyncio.gather(self._startup_task, return_exceptions=True)
            tasks = [task for task in (
                Plugin._audio_task, Plugin._restore_task, Plugin._edid_task,
                Plugin._ac_task, Plugin._charge_task, Plugin._module_task, Plugin._tm_guard_task,
                Plugin._vibration_gain_task,
                Plugin._fan_task,
                self._tdp_diagnostic_task,
            ) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            while self._rpc_jobs or self._workers:
                await asyncio.gather(*tuple(self._rpc_jobs | self._workers), return_exceptions=True)
            for name in ("_audio_task", "_restore_task", "_edid_task", "_ac_task", "_charge_task",
                         "_module_task", "_tm_guard_task", "_vibration_gain_task", "_fan_task"):
                setattr(Plugin, name, None)
            self._tdp_diagnostic_task = None
            try:
                await self._offload(fan_controller.close)
            except Exception as error:
                decky.logger.warning(f"{LOG} fan guard will retry automatic mode: {error}")
            with _lock:
                was_supported = bool(Plugin._state.get("supported", False))
                was_reconnecting = bool(Plugin._state.get("modules_reconnecting", False))
            if (was_supported and not was_reconnecting
                    and not getattr(settings, "recovery_error", "")
                    and await self._offload(companion_common.supported_device)):
                try:
                    connected = await self._offload(ec_backend.both_modules_connected)
                    if connected and not await self._offload(ec_backend.controller_powered):
                        await self._offload(ec_backend.set_controller_power, True)
                except Exception as error:
                    decky.logger.warning(f"{LOG} could not restore controller power on unload: {error}")
            decky.logger.info(f"{LOG} unloaded")
        self._cleanup_task = asyncio.create_task(cleanup())
        await complete(self._cleanup_task)

    async def _uninstall(self):
        await complete(self._remove_owned())

    async def _remove_owned(self):
        await self._unload()
        is_supported = await self._offload(companion_common.supported_device)
        if is_supported:
            try:
                await self._offload(audio_backend.remove_audio_fix, False)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not remove audio firmware path: {error}")
        if is_supported and settings.getSetting("charge_bypass", False):
            try:
                await self._offload(ec_backend.write_charge_bypass, False)
                decky.logger.info(f"{LOG} restored automatic charging before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore charging before uninstall: {error}")
        if is_supported and settings.getSetting("cpu_boost", True) is False:
            try:
                await self._offload(power_backend.write_cpu_boost, True)
                decky.logger.info(f"{LOG} restored CPU Boost before uninstall")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore CPU Boost before uninstall: {error}")
        if is_supported and display_backend.display_script_owned(display_backend.LUA_TARGET):
            try:
                await self._offload(display_backend.remove_display_script)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not remove display definition: {error}")
        baseline = settings.getSetting("controller_baseline", None)
        if is_supported and isinstance(baseline, dict):
            baseline = controller_backend.normalize_controller(baseline)
            try:
                await self._offload(controller_backend.apply_controller, baseline)
                await self._offload(controller_backend.set_vibration_gain, baseline["ff_gain"])
                decky.logger.info(f"{LOG} restored the controller's own lighting and vibration")
            except Exception as error:
                decky.logger.warning(f"{LOG} could not restore lighting and vibration: {error}")
        if is_supported:
            try:
                await self._offload(controller_backend.program_rear_buttons, False)
            except Exception as error:
                decky.logger.warning(f"{LOG} could not clear LC1/RC1 bindings: {error}")
        if is_supported:
            await self._offload(input_backend.remove_button_fix)
            await self._offload(_systemctl, "restart", "inputplumber")
