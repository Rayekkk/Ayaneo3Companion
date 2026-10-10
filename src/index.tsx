// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, ConfirmModal, DropdownItem, Field, gamepadSliderClasses, PanelSection, PanelSectionRow, Router, showModal, SliderField, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, ReactElement, useCallback, useEffect, useRef, useState } from "react";
import { PageShell, SectionHeader, SectionLink, StackedAction } from "./ui";
import { resetUpdates } from "./updates";
import { AboutPage } from "./about";
import { AudioPage, BatteryPage, ButtonsPage, DisplayPage, MagicModulesPage, VibrationPage } from "./device_pages";
import { FanPage, resetFanStatus, useFanControl } from "./fan";
import type { BatteryStatus, Controller, GameProfile, Hsv, Preset, RgbMode, RunningGame, State, Tdp, Vibration } from "./model";

type SectionKey = "tdp" | "vibration" | "rgb" | "fan" | "battery" | "modules" | "audio" | "buttons" | "screen" | "about";
type ActionKey = "tdp" | "profile" | "cpu_boost" | "vibration" | "battery" | "modules" | "audio" | "buttons" | "screen";
// A write acknowledges its own settings; other fields in its fast response
// may predate the latest hardware observation. Full polls replace diagnostics.
const ACTION_FIELDS: Record<ActionKey, ReadonlyArray<keyof State>> = {
  tdp: ["active_app_id", "tdp", "tdp_preset", "tdp_profile_pending", "tdp_profile_error"],
  profile: ["active_app_id", "tdp", "tdp_preset", "tdp_profile_pending", "tdp_profile_error"],
  cpu_boost: ["cpu_boost", "cpu_boost_supported"],
  vibration: ["controller"],
  battery: ["charge_bypass", "charge_bypass_supported"],
  modules: ["modules_reconnecting", "modules_connected", "module_left", "module_right"],
  audio: ["audio_fix_enabled", "audio_fix_installed", "audio_calibration_available",
    "audio_calibration_last", "audio_profile", "audio_fix_error"],
  buttons: ["button_fix_installed", "tm_guard_enabled", "tm_guard_status", "tm_guard_recoveries"],
  screen: ["screen_installed"],
};
interface Tuning { spl: number; spptOff: number; fpptOff: number }

const getState = callable<[], State>("get_state");
const getBatteryStatus = callable<[], BatteryStatus>("get_battery_status");
const setTdp = callable<[Tdp, Preset, string], State>("set_tdp");
const setCpuBoost = callable<[boolean], State>("set_cpu_boost");
const getGameProfile = callable<[string], GameProfile>("get_game_profile");
const setGameProfile = callable<[string, Tdp, Preset, string], State>("set_game_profile");
const deleteGameProfile = callable<[string, string], State>("delete_game_profile");
const setActiveApp = callable<[string], void>("set_active_app");
const setController = callable<[Partial<Controller>], State>("set_controller");
const setControllerWithVibrationFeedback = callable<[Partial<Controller>], State>("set_controller_with_vibration_feedback");
const setVibrationGain = callable<[number], State>("set_vibration_gain");
const testVibration = callable<[number], { success: boolean; error?: string }>("test_vibration");
const setChargeBypass = callable<[boolean], State>("set_charge_bypass");
const ejectModules = callable<["left" | "right" | "both"], State>("eject_modules");
const resetModules = callable<[], State>("reset_modules");
const setScreenFix = callable<[boolean, boolean?], State>("set_screen_fix");
const setButtonFix = callable<[boolean], State>("set_button_fix");
const setTmGuard = callable<[boolean], State>("set_tm_guard");
const setAudioFix = callable<[boolean], State>("set_audio_fix");
const reapplyAudioFix = callable<[], State>("reapply_audio_fix");
const recalibrateAudio = callable<[], State>("recalibrate_audio");
const PRESET_ORDER: Preset[] = ["Minimum", "Low power", "Balanced", "Performance", "Max", "Custom"];
const VIBRATION_LEVELS: Vibration[] = ["off", "low", "medium", "high"];
const VIBRATION_TEST_MS = 500;
const VIBRATION_APPLY_DELAY_MS = 150;
const rgbOptions = ["off", "solid", "pulse", "rainbow"].map(data => ({ data, label: data[0].toUpperCase() + data.slice(1) }));
const titleCase = (value: string) => value ? value[0].toUpperCase() + value.slice(1) : value;
const tdpStatusStyle = (message: string) => ({
  fontSize: "12px",
  color: message.startsWith("Error") ? "var(--gpColor-Red, #f87171)" : "var(--gpColor-Green, #4ade80)",
});

type GameListener = (game: RunningGame | null) => void;
class AppWatcher {
  private static listeners: GameListener[] = [];
  private static current: RunningGame | null = null;
  private static timer: ReturnType<typeof setInterval> | undefined;
  private static unsubs: Array<() => void> = [];
  private static started = false;
  private static busy = false;
  private static pending = false;
  private static lastPush = 0;
  private static generation = 0;
  private static lifetimeTimer: ReturnType<typeof setTimeout> | undefined;

  static activeGame(): RunningGame | null {
    try {
      const app = (Router as any)?.MainRunningApp;
      return app?.appid ? { appId: String(app.appid), name: app.display_name ?? String(app.appid) } : null;
    } catch { return null; }
  }
  static currentGame() { return this.current; }
  static listen(fn: GameListener) {
    this.listeners.push(fn);
    return () => { this.listeners = this.listeners.filter(item => item !== fn); };
  }
  static start() {
    if (this.started) return;
    this.started = true;
    const generation = ++this.generation;
    this.busy = false;
    this.pending = false;
    this.current = this.activeGame();

    try {
      const registration = (window as any).SteamClient?.GameSessions
        ?.RegisterForAppLifetimeNotifications?.(() => {
          // Router.MainRunningApp updates shortly after Steam's notification.
          if (!this.started || generation !== this.generation) return;
          if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
          this.lifetimeTimer = setTimeout(() => {
            this.lifetimeTimer = undefined;
            if (this.started && generation === this.generation) void this.check();
          }, 300);
        });
      if (registration?.unregister) this.unsubs.push(() => registration.unregister());
    } catch (error) {
      console.warn("[ayaneo3companion] app lifetime notifications unavailable", error);
    }

    this.timer = setInterval(() => void this.check(), 2000);
    void this.check(true);
  }
  static stop() {
    ++this.generation;
    this.busy = false;
    this.pending = false;
    if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
    this.lifetimeTimer = undefined;
    if (this.timer) clearInterval(this.timer);
    for (const unsubscribe of this.unsubs) {
      try { unsubscribe(); } catch { /* subscription may already be gone */ }
    }
    this.timer = undefined; this.unsubs = []; this.listeners = []; this.current = null; this.started = false; this.lastPush = 0;
  }
  private static async check(force = false) {
    if (!this.started) return;
    const generation = this.generation;
    const game = this.activeGame();
    const changed = game?.appId !== this.current?.appId;
    this.current = game;
    if (changed) this.listeners.forEach(listener => listener(game));
    // Keep local game context responsive while the backend applies a profile.
    // Only the latest observed context needs a subsequent serialized write.
    if (this.busy) { if (force || changed) this.pending = true; return; }
    const now = Date.now();
    if (force || changed || this.pending || now - this.lastPush >= 6000) {
      this.pending = false;
      this.busy = true;
      try { await setActiveApp(game?.appId ?? ""); if (generation === this.generation) this.lastPush = Date.now(); }
      catch (error) { console.error("[ayaneo3companion] active app update failed", error); }
      finally {
        if (generation === this.generation) {
          this.busy = false;
          if (this.pending || this.activeGame()?.appId !== this.current?.appId) void this.check(true);
        }
      }
    }
  }
}

function hsvToHex(hue: number, saturation: number): string {
  const h = ((hue % 360) + 360) % 360;
  const s = clamp(saturation, 0, 100) / 100;
  const c = s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = 1 - c;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [r, g, b].map(channel => Math.round((channel + m) * 255).toString(16).padStart(2, "0")).join("");
}

function hexToHsv(hex: string): { hue: number; saturation: number } {
  const value = hex.replace("#", "").padEnd(6, "0").slice(0, 6);
  const [r, g, b] = [0, 2, 4].map(offset => parseInt(value.slice(offset, offset + 2), 16) / 255);
  if (![r, g, b].every(Number.isFinite)) return { hue: 0, saturation: 100 };
  const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min;
  if (delta === 0) return { hue: 0, saturation: 0 };
  const hue = max === r ? 60 * (((g - b) / delta) % 6)
    : max === g ? 60 * ((b - r) / delta + 2) : 60 * ((r - g) / delta + 4);
  return { hue: Math.round((hue + 360) % 360), saturation: Math.round(delta / max * 100) };
}

interface SlowSliderProps {
  label: string; value: number; min: number; max: number; className: string; valueSuffix: string;
  disabled?: boolean;
  onChange(value: number): void; onChangeEnd(value: number): void;
}

// The parent owns the debounce and write queue, so leaving the RGB page cannot
// discard a pending slider value or send an older sibling slider closure.
const SlowSliderField: FC<SlowSliderProps> = slider => <SliderField
  label={slider.label} disabled={slider.disabled} value={slider.value} min={slider.min} max={slider.max}
  validValues="range" showValue valueSuffix={slider.valueSuffix} className={slider.className}
  onChange={value => { if (!slider.disabled) { slider.onChange(value); slider.onChangeEnd(value); } }}
/>;

function detectPreset(tdp: Tdp, presets: Record<string, Tdp>): Preset {
  for (const name of ["Minimum", "Low power", "Balanced", "Performance", "Max"] as Preset[]) {
    const value = presets[name];
    if (value && value.spl === tdp.spl && value.sppt === tdp.sppt && value.fppt === tdp.fppt) return name;
  }
  return "Custom";
}

function profileLabel(tdp: Tdp, stored: Preset | undefined, presets: Record<string, Tdp>): string {
  const selected = stored ?? detectPreset(tdp, presets);
  return selected === "Custom"
    ? `Custom (${tdp.spl} +${tdp.sppt - tdp.spl}/+${tdp.fppt - tdp.spl})`
    : selected;
}

const finite = (value: number, fallback: number) => Number.isFinite(value) ? value : fallback;
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
const spptOffsetMax = (spl: number) => Math.max(0, 37 - spl);
const fpptOffsetMax = (spl: number) => Math.max(0, 37 - spl);
const fromAbsolute = (tdp: Tdp): Tuning => ({
  spl: tdp.spl,
  spptOff: Math.max(0, tdp.sppt - tdp.spl),
  fpptOff: Math.max(0, tdp.fppt - tdp.spl),
});
const absolute = (tuning: Tuning): Tdp => ({
  spl: tuning.spl,
  sppt: tuning.spl + tuning.spptOff,
  fppt: tuning.spl + tuning.fpptOff,
});
function normalise(tuning: Tuning): Tuning {
  const spl = clamp(finite(tuning.spl, 15), 5, 35);
  const spptMax = spptOffsetMax(spl);
  const fpptMax = fpptOffsetMax(spl);
  const spptOff = clamp(finite(tuning.spptOff, 0), 0, spptMax);
  const fpptOff = Math.max(clamp(finite(tuning.fpptOff, spptOff), 0, fpptMax), spptOff);
  return { spl, spptOff, fpptOff };
}

let confirmedState: State | null = null;

const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const mounted = useRef(true);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const revision = useRef(0);
  const reading = useRef(false);
  const flushPendingController = useRef<() => void>(() => {});
  const desiredController = useRef<Controller | null>(null);
  const desiredRgb = useRef<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; ++revision.current; flushPendingController.current(); };
  }, []);
  const wasVisible = useRef(false);
  const transientOverlay = useRef(false);
  const presetInitialized = useRef(false);
  const tdpDirty = useRef(false);
  const desiredTdp = useRef<Tdp | null>(confirmedState?.tdp ?? null);
  const controllerDirty = useRef(false);
  const controllerPending = useRef<Controller | null>(null);
  const controllerPatch = useRef<Partial<Controller>>({});
  const controllerWriting = useRef(false);
  const controllerDrain = useRef<Promise<boolean> | null>(null);
  const controllerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastRgbMode = useRef<RgbMode>("solid");
  const [state, setState] = useState<State | null>(confirmedState);
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const fan = useFanControl(visible, activeSection === "fan");
  const [preset, setPreset] = useState<Preset>(() => confirmedState ? confirmedState.tdp_preset ?? detectPreset(confirmedState.tdp, confirmedState.presets) : "Custom");
  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null);
  const pendingActionRef = useRef<ActionKey | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [stateReadError, setStateReadError] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [game, setGame] = useState<RunningGame | null>(AppWatcher.currentGame());
  const [perGame, setPerGame] = useState(false);
  const [profileLoading, setProfileLoading] = useState(true);
  const [savedGamePreset, setSavedGamePreset] = useState<Preset | undefined>(undefined);
  const [battery, setBattery] = useState<BatteryStatus | null>(null);
  const gameRequest = useRef(0);
  const profileReady = useRef(!game);
  const contextStateReady = useRef(false);
  const busy = pendingAction !== null || Boolean(state?.settings_error || state?.initializing);
  const controllerBusy = busy || state?.controller_supported === false;
  const currentGameId = useRef(game?.appId ?? "");
  currentGameId.current = game?.appId ?? "";
  const tdpContextMismatch = state?.active_app_id !== currentGameId.current;
  const tdpBusy = busy || profileLoading || tdpContextMismatch || state?.tdp_supported === false || Boolean(state?.tdp_profile_pending);
  const tdpProblem = tdpContextMismatch
    ? "Waiting to confirm the current game's profile. TDP changes are blocked until synchronization succeeds. Other hardware controls remain available."
    : state?.tdp_profile_error || state?.tdp_error || "";
  const validContext = (context: string) => mounted.current && currentGameId.current === context && (AppWatcher.activeGame()?.appId ?? "") === context;
  const notifyFailure = (title: string, error: unknown) => {
    const body = error instanceof Error ? error.message : String(error);
    try { toaster.toast({ title, body }); } catch { console.error(`[ayaneo3companion] ${title}: ${body}`); }
  };
  const acceptState = (response: State, fields?: ReadonlyArray<keyof State>) => {
    if (!mounted.current) return;
    const next = fields && confirmedState
      ? { ...confirmedState, ...Object.fromEntries(fields.map(key => [key, response[key]])) }
      : response;
    const matchingGame = next.active_app_id === currentGameId.current;
    confirmedState = next;
    if (matchingGame && !tdpDirty.current) desiredTdp.current = next.tdp;
    setState(current => current ? { ...next,
      // Health and independent hardware controls remain observable when the
      // game transition fails. Only its TDP state waits for matching context.
      tdp: tdpDirty.current || !matchingGame ? current.tdp : next.tdp,
      tdp_preset: matchingGame ? next.tdp_preset : current.tdp_preset,
      controller: controllerDirty.current ? current.controller : next.controller,
    } : next);
  };

  useEffect(() => {
    if (!state || controllerDirty.current) return;
    const hsv = hexToHsv(state.controller.color);
    desiredRgb.current = { ...hsv, brightness: state.controller.brightness };
    setRgbEdit(desiredRgb.current);
  }, [state?.controller.color, state?.controller.brightness]);

  useEffect(() => {
    if (state && !controllerDirty.current) {
      desiredController.current = state.controller;
      if (state.controller.rgb_mode !== "off") lastRgbMode.current = state.controller.rgb_mode;
    }
  }, [state?.controller]);

  useEffect(() => AppWatcher.listen(next => {
    ++revision.current;
    tdpDirty.current = false;
    desiredTdp.current = null;
    presetInitialized.current = false;
    currentGameId.current = next?.appId ?? "";
    profileReady.current = !next;
    contextStateReady.current = false;
    setGame(next);
    setProfileLoading(true);
  }), []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !visibleRef.current || reading.current || pendingActionRef.current || controllerWriting.current || controllerPending.current) return;
    reading.current = true;
    const request = revision.current;
    try {
      const next = await getState();
      if (!mounted.current || !visibleRef.current || request !== revision.current) return;
      acceptState(next);
      setStateReadError(null);
      contextStateReady.current = next.active_app_id === currentGameId.current && !next.tdp_profile_pending;
      setProfileLoading(!contextStateReady.current || !profileReady.current);
      if (contextStateReady.current && !presetInitialized.current && !tdpDirty.current && !pendingActionRef.current) {
        presetInitialized.current = true;
        setPreset(next.tdp_preset ?? detectPreset(next.tdp, next.presets));
      }
    } catch (error) {
      if (mounted.current && visibleRef.current && request === revision.current) {
        const message = error instanceof Error ? error.message : String(error);
        if (confirmedState) setStateReadError(message);
        else setStatus(`Error: ${message}`);
      }
    } finally {
      reading.current = false;
      if (mounted.current && visibleRef.current && request !== revision.current) void refresh();
    }
  }, []);

  useEffect(() => {
    const request = ++gameRequest.current;
    const context = game?.appId ?? "";
    let retry: ReturnType<typeof setTimeout> | undefined;
    if (!tdpDirty.current) setPerGame(false);
    setSavedGamePreset(undefined);
    if (!visible || !game) {
      if (!game) { profileReady.current = true; setProfileLoading(!contextStateReady.current); }
      return;
    }
    profileReady.current = false;
    setProfileLoading(true);
    const current = () => request === gameRequest.current && validContext(context) && visibleRef.current;
    const lookup = async () => {
      if (!current()) return;
      try {
        const profile = await getGameProfile(context);
        if (!current()) return;
        profileReady.current = true;
        setProfileLoading(!contextStateReady.current);
        if (!tdpDirty.current) setPerGame(profile.exists);
        if (profile.exists) {
          const stored = profile.preset ?? detectPreset(profile.profile, confirmedState?.presets ?? {});
          setSavedGamePreset(stored);
          if (!tdpDirty.current) setPreset(stored);
        }
      } catch (error) {
        if (current()) {
          setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
          retry = setTimeout(() => { retry = undefined; void lookup(); }, 3000);
        }
      }
    };
    void lookup();
    return () => { gameRequest.current += 1; if (retry) clearTimeout(retry); };
  }, [game?.appId, visible]);

  useEffect(() => {
    if (visible && !wasVisible.current) {
      if (transientOverlay.current) {
        transientOverlay.current = false;
      } else {
        setActiveSection(null);
        setStatus(null);
      }
    }
    wasVisible.current = visible;
    ++revision.current;
    if (!visible) { flushPendingController.current(); return; }
    void refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh, visible]);

  useEffect(() => {
    if (!visible || activeSection !== "battery") return;
    let cancelled = false;
    let readingBattery = false;
    const updateBattery = async () => {
      if (cancelled || readingBattery) return;
      readingBattery = true;
      try {
        const next = await getBatteryStatus();
        if (!cancelled) setBattery(next);
      } catch (error) {
        console.warn("[ayaneo3companion] battery status unavailable", error);
      } finally { readingBattery = false; }
    };
    void updateBattery();
    const timer = setInterval(() => void updateBattery(), 10000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [activeSection, visible]);


  const run = async (action: ActionKey, work: () => Promise<State>, title: string, success?: string) => {
    if (pendingActionRef.current || state?.settings_error || state?.initializing) return;
    const context = currentGameId.current;
    const contextBound = action === "tdp" || action === "profile";
    const canAccept = () => mounted.current && (!contextBound || validContext(context));
    ++revision.current;
    if (action === "tdp" || action === "profile") ++gameRequest.current;
    pendingActionRef.current = action;
    setPendingAction(action); setStatus(null);
    try {
      const next = await work();
      if (canAccept()) { acceptState(next, ACTION_FIELDS[action]); if (success) setStatus(success); }
    } catch (error) {
      notifyFailure(title, error);
      if (canAccept()) setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      ++revision.current;
      pendingActionRef.current = null;
      if (mounted.current) { setPendingAction(null); void refresh(); }
    }
  };

  const openDropdown = (showMenu: () => void) => {
    transientOverlay.current = true;
    showMenu();
  };
  const openModal = (modal: ReactElement) => {
    transientOverlay.current = true;
    showModal(modal);
  };

  if (!state) return <PanelSection><PanelSectionRow>{status ? <Field label="Backend unavailable" description={status.replace(/^Error:\s*/, "")} /> : <Spinner />}</PanelSectionRow></PanelSection>;
  if (state.settings_error || state.startup_error || state.initializing) return <PanelSection title="Plugin unavailable"><PanelSectionRow><Field label={state.initializing ? "Starting hardware controls" : "Settings require attention"} description={state.settings_error || state.startup_error || "Waiting for the backend to finish initialising."} /></PanelSectionRow></PanelSection>;
  if (!state.supported) return <PanelSection title="Unsupported device"><PanelSectionRow><Field label={state.device} description="AYANEO 3 is required." /></PanelSectionRow></PanelSection>;

  const changeScreenFix = (enabled: boolean) => {
    const apply = (replaceExisting = false) => void run(
      "screen",
      () => setScreenFix(enabled, replaceExisting),
      "Display fix failed",
      enabled ? "Display definition installed. Restart Game Mode." : "Display definition removed. Restart Game Mode.",
    );
    if (enabled && state.screen_conflict) {
      openModal(
        <ConfirmModal
          strTitle="Replace Display Definition?"
          strDescription="Another AYANEO 3 gamescope display definition is already installed. OLED Fix must replace it to manage the display. The existing file will be backed up before any change."
          strOKButtonText="Replace"
          strCancelButtonText="Keep Existing"
          bDestructiveWarning
          onOK={() => apply(true)}
        />,
      );
      return;
    }
    apply();
  };

  const tdp = state.tdp;
  const tuning = normalise(fromAbsolute(tdp));
  const spptOff = tuning.spptOff;
  const fpptOff = tuning.fpptOff;
  const maxSpptOffset = spptOffsetMax(tuning.spl);
  const maxFpptOffset = fpptOffsetMax(tuning.spl);
  const currentTuning = () => normalise(fromAbsolute(desiredTdp.current ?? tdp));
  const setCustomTdp = (next: Tdp) => {
    if (tdpBusy || pendingActionRef.current || !validContext(game?.appId ?? "")) return;
    desiredTdp.current = next;
    tdpDirty.current = true;
    setPreset("Custom"); setState(current => current ? { ...current, tdp: next } : current); setStatus(null);
  };
  const setSpl = (value: number) => {
    if (!Number.isFinite(value)) return;
    setCustomTdp(absolute(normalise({ ...currentTuning(), spl: value })));
  };
  const setSpptOff = (value: number) => {
    if (!Number.isFinite(value)) return;
    const current = currentTuning();
    const nextSppt = clamp(value, 0, spptOffsetMax(current.spl));
    setCustomTdp(absolute(normalise({ ...current, spptOff: nextSppt, fpptOff: Math.max(current.fpptOff, nextSppt) })));
  };
  const setFpptOff = (value: number) => {
    if (!Number.isFinite(value)) return;
    const current = currentTuning();
    const nextFppt = clamp(value, 0, fpptOffsetMax(current.spl));
    setCustomTdp(absolute(normalise({ ...current, fpptOff: nextFppt, spptOff: Math.min(current.spptOff, nextFppt) })));
  };
  const toggleCpuBoost = (enabled: boolean) => run("cpu_boost", () => setCpuBoost(enabled), "CPU Boost failed");
  const choosePreset = async (name: Preset) => {
    const context = game?.appId ?? "";
    if (tdpBusy || pendingActionRef.current || !validContext(context)) return;
    const previousDirty = tdpDirty.current;
    const previousPreset = previousDirty ? "Custom" : preset;
    const previousTdp = desiredTdp.current ?? tdp;
    const previousSavedPreset = savedGamePreset;
    presetInitialized.current = true;
    setPreset(name); setStatus(null);
    if (name === "Custom") return;
    const value = state.presets[name];
    if (!value) return;
    ++revision.current;
    ++gameRequest.current;
    tdpDirty.current = false;
    desiredTdp.current = value;
    setState(current => current ? { ...current, tdp: value } : current);
    pendingActionRef.current = "tdp";
    setPendingAction("tdp");
    try {
      const next = await (perGame && game
        ? setGameProfile(game.appId, value, name, context)
        : setTdp(value, name, context));
      if (!validContext(context)) return;
      acceptState(next, ACTION_FIELDS.tdp);
      if (perGame && game) setSavedGamePreset(name);
      setStatus(perGame && game ? `${name} saved for ${game.name}.` : `${name} applied.`);
    } catch (error) {
      notifyFailure("TDP preset failed", error);
      if (validContext(context)) {
        tdpDirty.current = previousDirty;
        desiredTdp.current = previousTdp;
        setPreset(previousPreset);
        setSavedGamePreset(previousSavedPreset);
        setState(current => current ? { ...current, tdp: previousTdp } : current);
        setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      ++revision.current;
      pendingActionRef.current = null;
      if (mounted.current) { setPendingAction(null); void refresh(); }
    }
  };
  const togglePerGame = async (enabled: boolean) => {
    const context = game?.appId ?? "";
    if (!game || tdpBusy || pendingActionRef.current || !validContext(context)) return;
    ++revision.current;
    ++gameRequest.current;
    pendingActionRef.current = "profile";
    setPendingAction("profile");
    setPerGame(enabled); setStatus(null);
    try {
      if (enabled) {
        const profile = await getGameProfile(context);
        if (!validContext(context)) return;
        if (profile.exists) {
          const stored = profile.preset ?? detectPreset(profile.profile, state.presets);
          tdpDirty.current = false;
          desiredTdp.current = profile.profile;
          setSavedGamePreset(stored);
          setPreset(stored);
          setState(current => current ? { ...current, tdp: profile.profile } : current);
          setStatus(`Profile applied for ${game.name}.`);
        } else {
          setSavedGamePreset(undefined);
          setStatus(`No saved profile for ${game.name}. Use Custom or choose a preset to create one.`);
        }
      } else {
        const next = await deleteGameProfile(context, context);
        if (!validContext(context)) return;
        tdpDirty.current = false;
        setSavedGamePreset(undefined);
        acceptState(next, ACTION_FIELDS.profile); setPreset(next.tdp_preset ?? detectPreset(next.tdp, next.presets));
        setStatus("Switched to global settings.");
      }
    } catch (error) {
      notifyFailure(enabled ? "Game profile lookup failed" : "Game profile removal failed", error);
      if (validContext(context)) {
        setPerGame(!enabled);
        setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
      }
    } finally {
      ++revision.current;
      pendingActionRef.current = null;
      if (mounted.current) { setPendingAction(null); void refresh(); }
    }
  };
  const flushController = (): Promise<boolean> => {
    if (controllerDrain.current) return controllerDrain.current;
    if (!controllerPending.current) return Promise.resolve(true);
    controllerWriting.current = true;
    let needsRefresh = false;
    const unconfirmed = new Set<keyof Controller>();
    let resolveDrain!: (value: boolean) => void;
    let rejectDrain!: (error: unknown) => void;
    const drain = new Promise<boolean>((resolve, reject) => { resolveDrain = resolve; rejectDrain = reject; });
    controllerDrain.current = drain;
    ++revision.current;
    // Share completion of the entire queue, including its in-flight write,
    // while still starting the first RPC immediately when QAM is closed.
    const processQueue = async () => {
      try {
        while (controllerPending.current) {
          controllerPending.current = null;
          const patch = controllerPatch.current;
          controllerPatch.current = {};
          const firmwareChanged = patch.vibration !== undefined || patch.rgb_mode !== undefined
            || patch.color !== undefined || patch.brightness !== undefined;
          try {
            let applied: State | null = null;
            if (firmwareChanged) {
              applied = patch.vibration !== undefined
                ? await setControllerWithVibrationFeedback(patch)
                : await setController(patch);
            }
            // Mixed edits commit firmware and FF_GAIN in one transaction;
            // gain alone still avoids an unnecessary HID write.
            if (patch.ff_gain !== undefined && !firmwareChanged) applied = await setVibrationGain(patch.ff_gain);
            // A later independent patch does not confirm fields that failed
            // earlier in this drain. An explicit newer edit of that same field
            // can still supersede the failure and become confirmed.
            for (const field of Object.keys(patch) as Array<keyof Controller>) unconfirmed.delete(field);
            if (!controllerPending.current) {
              controllerDirty.current = false;
              if (applied && mounted.current) {
                confirmedState = confirmedState ? { ...confirmedState, controller: applied.controller } : applied;
                setState(current => current
                  ? { ...current, controller: applied.controller }
                  : applied);
              }
            }
          } catch (error) {
            for (const field of Object.keys(patch) as Array<keyof Controller>) unconfirmed.add(field);
            const message = error instanceof Error ? error.message : String(error);
            notifyFailure("Controller setting failed", message);
            if (!controllerPending.current) {
              controllerDirty.current = false;
              desiredController.current = null;
              needsRefresh = true;
            }
          }
        }
      } finally {
        ++revision.current;
        controllerWriting.current = false;
        controllerDrain.current = null;
        if (controllerPending.current) void flushController();
        else if (needsRefresh && mounted.current) void refresh();
      }
      return unconfirmed.size === 0;
    };
    void processQueue().then(resolveDrain, rejectDrain);
    return drain;
  };
  const applyController = (part: Partial<Controller>, delay = 0) => {
    if (pendingActionRef.current || state.settings_error || state.initializing) return;
    ++revision.current;
    const base = controllerPending.current ?? desiredController.current ?? state.controller;
    const controller = { ...base, ...part };
    if (controller.rgb_mode !== "off") lastRgbMode.current = controller.rgb_mode;
    controllerDirty.current = true;
    controllerPending.current = controller;
    controllerPatch.current = { ...controllerPatch.current, ...part };
    desiredController.current = controller;
    if (mounted.current) setState(current => current ? { ...current, controller } : current);
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    if (!mounted.current) { controllerTimer.current = null; void flushController(); }
    else controllerTimer.current = setTimeout(() => { controllerTimer.current = null; void flushController(); }, delay);
  };
  flushPendingController.current = () => {
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    controllerTimer.current = null;
    if (controllerPending.current) void flushController();
  };
  const setVibrationLevel = (value: number) => {
    if (!Number.isFinite(value)) return;
    const vibration = VIBRATION_LEVELS[Math.round(clamp(value, 0, VIBRATION_LEVELS.length - 1))];
    const current = controllerPending.current ?? desiredController.current ?? state.controller;
    if (vibration !== current.vibration) applyController({ vibration }, VIBRATION_APPLY_DELAY_MS);
  };
  const setFfGain = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const value = Math.round(clamp(raw, 0, 100) / 10) * 10;
    const current = controllerPending.current ?? desiredController.current ?? state.controller;
    if (value !== current.ff_gain) applyController({ ff_gain: value }, 150);
  };
  const previewRgb = (next: Hsv) => {
    controllerDirty.current = true;
    desiredRgb.current = next;
    setRgbEdit(next);
  };
  const commitRgb = (next: Hsv, field: "color" | "brightness") => {
    desiredRgb.current = next;
    setRgbEdit(next);
    applyController(field === "color" ? { color: hsvToHex(next.hue, next.saturation) } : { brightness: next.brightness }, 500);
  };
  const runVibrationTest = async () => {
    if (pendingActionRef.current || state.settings_error || state.initializing) return;
    ++revision.current;
    pendingActionRef.current = "vibration";
    setPendingAction("vibration");
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    controllerTimer.current = null;
    let controllerConfirmed = false;
    try {
      controllerConfirmed = await flushController();
      if (!controllerConfirmed || !mounted.current) return;
      const result = await testVibration(VIBRATION_TEST_MS);
      if (!result.success) notifyFailure("Vibration test failed", result.error ?? "Unknown error");
    } catch (error) {
      notifyFailure("Vibration test failed", error);
    } finally {
      pendingActionRef.current = null;
      if (mounted.current) {
        setPendingAction(null);
        if (!controllerConfirmed) void refresh();
      }
    }
  };

  const openSection = (section: SectionKey) => { setStatus(null); setActiveSection(section); };
  const backToControls = () => { setStatus(null); setActiveSection(null); };
  const moduleSummary = state.modules_reconnecting
    ? "Waiting for both modules"
    : state.modules_connected
      ? `${state.module_left.label} · ${state.module_right.label}`
      : "A module is disconnected";
  const audioSummary = state.audio_fix_enabled
    ? state.audio_fix_installed ? state.audio_profile : "Tuning pending"
    : "Generic SteamOS profile";
  const readFailure = stateReadError && <PanelSection title="Device Status">
    <PanelSectionRow><Field focusable label="Status refresh failed"
      description={`Showing last confirmed values. Retrying automatically. ${stateReadError}`} /></PanelSectionRow>
  </PanelSection>;

  if (!activeSection) return <PageShell>
    {readFailure}
    <PanelSection title="Hardware Controls">
      <SectionLink title="TDP" description={tdpProblem || `${preset} · ${tdp.spl} / ${tdp.sppt} / ${tdp.fppt} W`} onClick={() => openSection("tdp")} />
      <SectionLink title="Vibration" description={`${titleCase(state.controller.vibration)} · FF Gain ${state.controller.ff_gain}%`} onClick={() => openSection("vibration")} />
      <SectionLink title="RGB" description={state.controller.rgb_mode === "off" ? "Off" : `${titleCase(state.controller.rgb_mode)} · #${state.controller.color.toUpperCase()}`} onClick={() => openSection("rgb")} />
      {fan.available && <SectionLink title="Fan Control"
        description={fan.error || !fan.status?.supported ? "Status unavailable" : `${fan.status.mode === "manual" && fan.status.percent != null ? `Manual · ${fan.status.percent}%` : "Auto"}${fan.status.rpm == null ? "" : ` · ${fan.status.rpm} RPM`}`}
        statusNote={fan.error || undefined} onClick={() => openSection("fan")} />}
      <SectionLink title="Battery" description={state.charge_bypass ? "Bypass charging active" : "Automatic charging"} onClick={() => openSection("battery")} />
      <SectionLink title="Magic Modules" description={moduleSummary} onClick={() => openSection("modules")} />
      <SectionLink title="Audio" description={audioSummary} onClick={() => openSection("audio")} />
      <SectionLink title="Key Binding" description={`${state.button_fix_installed ? "L5/R5 enabled" : "Native mapping"} · TM Guard ${state.tm_guard_enabled ? "on" : "off"}`} onClick={() => openSection("buttons")} />
      <SectionLink title="OLED Display" description={state.screen_installed ? `Definition installed · ${state.edid_game_nits || 800} nits` : state.screen_conflict ? "Existing definition requires confirmation" : "Display definition not installed"} onClick={() => openSection("screen")} />
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow><Field focusable label={state.device} description={`SteamOS · TDP backend: ${state.tdp_backend}`} /></PanelSectionRow>
    </PanelSection>
    <PanelSection title="Plugin">
      <SectionLink title="About" description={`AYANEO 3 Companion · v${state.version}`} onClick={() => openSection("about")} />
    </PanelSection>
  </PageShell>;

  return <PageShell>
    <SectionHeader title={activeSection === "modules" ? "Magic Modules" : activeSection === "buttons" ? "Key Binding" : activeSection === "screen" ? "OLED Display" : activeSection === "fan" ? "Fan Control" : titleCase(activeSection)} onBack={backToControls} />
    {readFailure}

    {activeSection === "tdp" && <>
      <PanelSection title="Current TDP">
        <PanelSectionRow><Field label={tdpContextMismatch ? "Game profile not synchronized" : state.tdp_profile_pending ? "TDP profile pending" : state.tdp_supported === false ? "TDP control unavailable" : `${tdp.spl} / ${tdp.sppt} / ${tdp.fppt} W`} description={tdpProblem || `SPL / SPPT / FPPT · ${state.gpu_power_w == null ? "power unavailable" : `${state.gpu_power_w.toFixed(1)} W currently`} · ${state.tdp_backend}`} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="CPU">
        <PanelSectionRow><ToggleField
          label="CPU Boost"
          description={state.cpu_boost_supported ? "Allow the CPU to boost above its base frequency when performance and power limits permit." : "CPU Boost control is unavailable on this kernel."}
          checked={state.cpu_boost}
          disabled={busy || !state.cpu_boost_supported}
          onChange={enabled => void toggleCpuBoost(enabled)}
        /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Game Profile">
        <PanelSectionRow><ToggleField
          label="Per Game Profile"
          description={game ? (perGame ? (
            <span style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
              <span>{game.name}</span>
              <span>
                <span style={{ fontSize: "11px", color: "var(--gpColor-TextMuted, rgba(255,255,255,0.5))" }}>Profile: </span>
                <span style={{ fontSize: "11px", fontWeight: "bold", color: "var(--gpColor-White, #fff)", background: "rgba(74,222,128,0.25)", borderRadius: "4px", padding: "1px 6px" }}>
                  {profileLabel(tdp, savedGamePreset, state.presets)}
                </span>
              </span>
            </span>
          ) : game.name) : "No game running"}
          checked={perGame}
          disabled={!game || tdpBusy}
          onChange={enabled => void togglePerGame(enabled)}
        /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Preset">
        {PRESET_ORDER.map(name => <PanelSectionRow key={name}>
          <ButtonItem
            layout="below"
            disabled={preset === name || tdpBusy}
            onClick={() => void choosePreset(name)}
          >
            {preset === name ? `> ${name}` : name}
          </ButtonItem>
        </PanelSectionRow>)}
        {status && preset !== "Custom" && <PanelSectionRow><div style={tdpStatusStyle(status)}>{status}</div></PanelSectionRow>}
      </PanelSection>
      {preset === "Custom" && <>
        <PanelSection title="TDP Limits">
          <PanelSectionRow><SliderField label={`SPL (TDP) - ${tuning.spl} W`} value={tuning.spl} min={5} max={35} step={1} disabled={tdpBusy} onChange={setSpl} description="Sustained power limit - the main TDP dial" /></PanelSectionRow>
          <PanelSectionRow><SliderField key={`sppt-${tuning.spl}-${maxSpptOffset}`} label={`SPPT +${spptOff} W  =  ${tuning.spl + spptOff} W`} value={spptOff} min={0} max={maxSpptOffset || 1} step={1} disabled={tdpBusy || maxSpptOffset === 0} onChange={setSpptOff} description={maxSpptOffset === 0 ? "No headroom left at this SPL" : `Slow limit headroom above SPL (max +${maxSpptOffset} W here)`} /></PanelSectionRow>
          <PanelSectionRow><SliderField key={`fppt-${tuning.spl}-${maxFpptOffset}`} label={`FPPT +${fpptOff} W  =  ${tuning.spl + fpptOff} W`} value={fpptOff} min={0} max={maxFpptOffset || 1} step={1} disabled={tdpBusy || maxFpptOffset === 0} onChange={setFpptOff} description={maxFpptOffset === 0 ? "No headroom left at this SPL" : `Fast limit headroom above SPL (max +${maxFpptOffset} W here)`} /></PanelSectionRow>
        </PanelSection>
        <PanelSection title="Action">
          <PanelSectionRow><ButtonItem layout="below" disabled={tdpBusy} onClick={() => {
            const context = game?.appId ?? "";
            if (tdpBusy || !validContext(context)) return;
            const value = desiredTdp.current ?? tdp;
            void run("tdp", async () => {
              const next = await (perGame && game
                ? setGameProfile(game.appId, value, "Custom", context)
                : setTdp(value, "Custom", context));
              if (validContext(context)) {
                tdpDirty.current = false;
                if (perGame && game) setSavedGamePreset("Custom");
              }
              return next;
            }, "TDP apply failed", perGame && game ? `Custom settings saved for ${game.name}.` : "Custom settings applied.");
          }}>{pendingAction === "tdp" ? "Applying..." : perGame && game ? `Apply & Save for ${game.name}` : "Apply TDP"}</ButtonItem></PanelSectionRow>
          {status && <PanelSectionRow><div style={tdpStatusStyle(status)}>{status}</div></PanelSectionRow>}
        </PanelSection>
      </>}
    </>}

    {activeSection === "vibration" && <VibrationPage state={state} busy={busy} testing={pendingAction === "vibration"} onLevel={setVibrationLevel} onGain={setFfGain} onTest={() => void runVibrationTest()} />}

    {activeSection === "fan" && <FanPage status={fan.status} available={fan.available} error={fan.error} busy={fan.busy} onApply={(mode, percent) => void fan.apply(mode, percent)} />}

    {activeSection === "rgb" && <PanelSection title="LED Settings">
      {state.controller_error && <PanelSectionRow><Field label="Controller control unavailable" description={state.controller_error} /></PanelSectionRow>}
      <PanelSectionRow><ToggleField label="Enable LED Control" disabled={controllerBusy} checked={state.controller.rgb_mode !== "off"} onChange={enabled => applyController({ rgb_mode: enabled ? lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED Mode" disabled={controllerBusy} selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onMenuWillOpen={openDropdown} onChange={option => applyController({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
        <PanelSectionRow><Field label={`#${state.controller.color.toUpperCase()}`} description={`${titleCase(state.controller.rgb_mode)} · ${rgbEdit.brightness}% brightness`}><span style={{ display: "block", width: "28px", height: "28px", borderRadius: "50%", background: `#${state.controller.color}`, border: "2px solid rgba(255,255,255,.55)" }} /></Field></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Hue" disabled={controllerBusy} value={rgbEdit.hue} min={0} max={359} valueSuffix="°" className="AyaneoRgbHue" onChange={hue => previewRgb({ ...desiredRgb.current, hue })} onChangeEnd={hue => commitRgb({ ...desiredRgb.current, hue }, "color")} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Saturation" disabled={controllerBusy} value={rgbEdit.saturation} min={0} max={100} valueSuffix="%" className="AyaneoRgbSaturation" onChange={saturation => previewRgb({ ...desiredRgb.current, saturation })} onChangeEnd={saturation => commitRgb({ ...desiredRgb.current, saturation }, "color")} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Brightness" disabled={controllerBusy} value={rgbEdit.brightness} min={0} max={100} valueSuffix="%" className="AyaneoRgbBrightness" onChange={brightness => previewRgb({ ...desiredRgb.current, brightness })} onChangeEnd={brightness => commitRgb({ ...desiredRgb.current, brightness }, "brightness")} /></PanelSectionRow>
        <style>{`
          .AyaneoRgbHue .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, hsl(0,100%,50%), hsl(60,100%,50%), hsl(120,100%,50%), hsl(180,100%,50%), hsl(240,100%,50%), hsl(300,100%,50%), hsl(360,100%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
          .AyaneoRgbSaturation .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, hsl(${rgbEdit.hue},0%,100%), hsl(${rgbEdit.hue},100%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
          .AyaneoRgbBrightness .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, #000, hsl(${rgbEdit.hue},${rgbEdit.saturation}%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
        `}</style>
      </>}
    </PanelSection>}

    {activeSection === "battery" && <BatteryPage
      state={state} battery={battery} busy={busy}
      onBypass={enabled => void run("battery", () => setChargeBypass(enabled), "Charge bypass failed", enabled ? "Charging bypass enabled." : "Automatic charging restored.")}
    />}

    {activeSection === "modules" && <MagicModulesPage
      state={state} busy={busy} pending={pendingAction === "modules"}
      onEject={side => openModal(<ConfirmModal
        strTitle={`Eject ${titleCase(side)}`}
        strDescription={`Release ${side === "both" ? "both controller modules" : `the ${side} controller module`}?`}
        strOKButtonText="Eject" bDestructiveWarning
        onOK={() => void run("modules", () => ejectModules(side), "Module eject failed", `${titleCase(side)} module${side === "both" ? "s" : ""} released.`)}
      />)}
      onReset={() => openModal(<ConfirmModal
        strTitle="Reset Magic Modules"
        strDescription="The controller will briefly disconnect while both modules are reinitialised."
        strOKButtonText="Reset"
        onOK={() => void run("modules", () => resetModules(), "Magic Module reset failed", "Both modules reset and detected.")}
      />)}
    />}

    {activeSection === "audio" && <AudioPage
      state={state} busy={busy} pending={pendingAction === "audio"}
      onEnabled={enabled => void run("audio", () => setAudioFix(enabled), "Audio fix failed", enabled ? "AYANEO speaker tuning enabled." : "Generic audio profile restored.")}
      onReapply={() => void run("audio", () => reapplyAudioFix(), "Audio fix failed", "AYANEO speaker tuning reapplied.")}
      onRecalibrate={() => openModal(<ConfirmModal
        strTitle="Recalibrate Speakers"
        strDescription="Place the console on a clear surface in a quiet room. A calibration signal will play and the result will be written to EFI."
        strOKButtonText="Recalibrate" bDestructiveWarning
        onOK={() => void run("audio", () => recalibrateAudio(), "Audio recalibration failed", "Calibration saved. Restart the console to apply it.")}
      />)}
    />}

    {activeSection === "buttons" && <ButtonsPage
      state={state} busy={busy}
      onButtonFix={enabled => void run("buttons", () => setButtonFix(enabled), "Key binding fix failed", enabled ? "Key binding fix enabled." : "Native key mapping restored.")}
      onTmGuard={enabled => void run("buttons", () => setTmGuard(enabled), "TM Guard failed", enabled ? "TM Guard enabled." : "TM Guard disabled.")}
    />}

    {activeSection === "screen" && <DisplayPage state={state} busy={busy} onChange={changeScreenFix} />}

    {activeSection === "about" && <AboutPage version={state.version} />}

    {status && activeSection !== "tdp" && <PanelSection title="Status"><PanelSectionRow><Field label={status.startsWith("Error:") ? "Action failed" : "Done"} description={status.replace(/^Error:\s*/, "")} /></PanelSectionRow></PanelSection>}
  </PageShell>;
};

const Icon: FC = () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: "1em", height: "1em" }}><path d="M7 8h10a5 5 0 0 1 4.7 6.7l-1.1 3.1a2 2 0 0 1-3.3.8L15 16H9l-2.3 2.6a2 2 0 0 1-3.3-.8l-1.1-3.1A5 5 0 0 1 7 8Z"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="12" r="2"/></svg>;
export default definePlugin(() => {
  // Per-game profiles must keep following Steam even while the QAM is closed.
  AppWatcher.start();
  return {
    name: "AYANEO 3 Companion",
    titleView: <div className={staticClasses.Title}>AYANEO 3 Companion</div>,
    content: <Content />,
    alwaysRender: true,
    icon: <Icon />,
    onDismount() {
      AppWatcher.stop(); confirmedState = null;
      resetFanStatus();
      resetUpdates();
    },
  };
});
