// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, ConfirmModal, DropdownItem, Field, gamepadSliderClasses, PanelSection, PanelSectionRow, Router, showModal, SliderField, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { FaChevronRight } from "react-icons/fa";

type Vibration = "off" | "low" | "medium" | "high";
type RgbMode = "off" | "solid" | "pulse" | "rainbow";
type Preset = "Minimum" | "Low power" | "Balanced" | "Performance" | "Max" | "Custom";
type SectionKey = "tdp" | "vibration" | "rgb" | "battery" | "modules" | "audio" | "buttons" | "screen";
type ActionKey = "tdp" | "profile" | "vibration" | "battery" | "modules" | "audio" | "buttons" | "screen";
interface Tdp { spl: number; sppt: number; fppt: number }
interface Tuning { spl: number; spptOff: number; fpptOff: number }
interface Controller { vibration: Vibration; ff_gain: number; rgb_mode: RgbMode; color: string; brightness: number }
interface Hsv { hue: number; saturation: number; brightness: number }
interface RunningGame { appId: string; name: string }
interface GameProfile { exists: boolean; profile: Tdp }
interface ModuleInfo { code: number | null; label: string; layout: string; status: string; connected: boolean }
interface BatteryStatus { available: boolean; percent: number | null; status: string; seconds_to_full: number | null; power_w: number | null; source: "UPower" | "sysfs" | "none" }
interface State { supported: boolean; device: string; tdp_backend: string; tdp: Tdp; presets: Record<string, Tdp>; controller: Controller; gpu_power_w: number | null; screen_installed: boolean; edid_patched: boolean; edid_game_nits: number; button_fix_installed: boolean; charge_bypass_supported: boolean; charge_bypass: boolean; module_eject_supported: boolean; module_reset_supported: boolean; modules_reconnecting: boolean; modules_connected: boolean; module_left: ModuleInfo; module_right: ModuleInfo; tm_guard_enabled: boolean; tm_guard_status: string; tm_guard_recoveries: number; audio_fix_supported: boolean; audio_fix_enabled: boolean; audio_fix_installed: boolean; audio_profile: string; audio_fix_error: string; audio_calibration_available: boolean; audio_calibration_last: string }

const getState = callable<[], State>("get_state");
const getBatteryStatus = callable<[], BatteryStatus>("get_battery_status");
const setTdp = callable<[Tdp], State>("set_tdp");
const getGameProfile = callable<[string], GameProfile>("get_game_profile");
const setGameProfile = callable<[string, Tdp], State>("set_game_profile");
const deleteGameProfile = callable<[string], State>("delete_game_profile");
const setActiveApp = callable<[string], void>("set_active_app");
const setController = callable<[Controller], State>("set_controller");
const setControllerWithVibrationFeedback = callable<[Controller], State>("set_controller_with_vibration_feedback");
const setVibrationGain = callable<[number], State>("set_vibration_gain");
const testVibration = callable<[number], { success: boolean; error?: string }>("test_vibration");
const setChargeBypass = callable<[boolean], State>("set_charge_bypass");
const ejectModules = callable<["left" | "right" | "both"], State>("eject_modules");
const resetModules = callable<[], State>("reset_modules");
const setScreenFix = callable<[boolean], State>("set_screen_fix");
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
const presetOptions = PRESET_ORDER.map(data => ({ data, label: data }));
const titleCase = (value: string) => value ? value[0].toUpperCase() + value.slice(1) : value;
const formatDuration = (seconds: number): string => {
  const minutes = Math.max(1, Math.round(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  if (!hours) return `${minutes} min`;
  return remainder ? `${hours} hr ${remainder} min` : `${hours} hr`;
};

type GameListener = (game: RunningGame | null) => void;
class AppWatcher {
  private static listeners: GameListener[] = [];
  private static current: RunningGame | null = null;
  private static timer: ReturnType<typeof setInterval> | undefined;
  private static unsubs: Array<() => void> = [];
  private static started = false;
  private static busy = false;
  private static lastPush = 0;

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
    this.current = this.activeGame();

    try {
      const registration = (window as any).SteamClient?.GameSessions
        ?.RegisterForAppLifetimeNotifications?.(() => {
          // Router.MainRunningApp updates shortly after Steam's notification.
          setTimeout(() => void this.check(), 300);
        });
      if (registration?.unregister) this.unsubs.push(() => registration.unregister());
    } catch (error) {
      console.warn("[ayaneo3companion] app lifetime notifications unavailable", error);
    }

    this.timer = setInterval(() => void this.check(), 2000);
    void this.check(true);
  }
  static stop() {
    if (this.timer) clearInterval(this.timer);
    for (const unsubscribe of this.unsubs) {
      try { unsubscribe(); } catch { /* subscription may already be gone */ }
    }
    this.timer = undefined; this.unsubs = []; this.listeners = []; this.current = null; this.started = false; this.lastPush = 0;
  }
  private static async check(force = false) {
    if (this.busy) return;
    const game = this.activeGame();
    const changed = game?.appId !== this.current?.appId;
    this.current = game;
    const now = Date.now();
    if (force || changed || now - this.lastPush >= 6000) {
      this.busy = true;
      try { await setActiveApp(game?.appId ?? ""); this.lastPush = now; }
      catch (error) { console.error("[ayaneo3companion] active app update failed", error); }
      finally { this.busy = false; }
    }
    if (changed) this.listeners.forEach(listener => listener(game));
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
  onChange(value: number): void; onChangeEnd(value: number): void;
}

// Update the preview immediately, then send one hardware write after movement
// has stopped. Keeping the timer in a ref avoids the initial no-op write and
// stale-value races caused by an effect tied to render state.
const SlowSliderField: FC<SlowSliderProps> = slider => {
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const commit = useRef(slider.onChangeEnd);
  commit.current = slider.onChangeEnd;
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return <SliderField
    label={slider.label} value={slider.value} min={slider.min} max={slider.max}
    validValues="range" showValue valueSuffix={slider.valueSuffix} className={slider.className}
    onChange={value => {
      slider.onChange(value);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => { timer.current = null; commit.current(value); }, 500);
    }}
  />;
};

function detectPreset(tdp: Tdp, presets: Record<string, Tdp>): Preset {
  for (const name of ["Minimum", "Low power", "Balanced", "Performance", "Max"] as Preset[]) {
    const value = presets[name];
    if (value && value.spl === tdp.spl && value.sppt === tdp.sppt && value.fppt === tdp.fppt) return name;
  }
  return "Custom";
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

const SectionLink: FC<{ title: string; description: string; onClick: () => void }> = ({ title, description, onClick }) => (
  <PanelSectionRow>
    <Field label={title} description={description} childrenLayout="inline" childrenContainerWidth="min" focusable highlightOnFocus onActivate={onClick}>
      <FaChevronRight aria-hidden style={{ display: "block", flexShrink: 0 }} />
    </Field>
  </PanelSectionRow>
);

const PageShell: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" }}>
    {children}
  </div>
);

const SectionHeader: FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => (
  <PanelSection title={title}>
    <PanelSectionRow>
      <ButtonItem layout="below" onClick={onBack}>‹ All Controls</ButtonItem>
    </PanelSectionRow>
  </PanelSection>
);

const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const wasVisible = useRef(false);
  const presetInitialized = useRef(false);
  const tdpDirty = useRef(false);
  const controllerDirty = useRef(false);
  const controllerPending = useRef<Controller | null>(null);
  const controllerWriting = useRef(false);
  const controllerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ffGainTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ffGainPending = useRef<number | null>(null);
  const ffGainDirty = useRef(false);
  const appliedVibration = useRef<Vibration | null>(null);
  const lastRgbMode = useRef<RgbMode>("solid");
  const [state, setState] = useState<State | null>(null);
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [preset, setPreset] = useState<Preset>("Custom");
  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null);
  const pendingActionRef = useRef<ActionKey | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [game, setGame] = useState<RunningGame | null>(AppWatcher.currentGame());
  const [perGame, setPerGame] = useState(false);
  const [battery, setBattery] = useState<BatteryStatus | null>(null);
  const gameRequest = useRef(0);
  const busy = pendingAction !== null;

  useEffect(() => {
    if (!state || controllerDirty.current) return;
    const hsv = hexToHsv(state.controller.color);
    setRgbEdit({ ...hsv, brightness: state.controller.brightness });
  }, [state?.controller.color, state?.controller.brightness]);

  useEffect(() => {
    if (state && appliedVibration.current === null && !controllerDirty.current) {
      appliedVibration.current = state.controller.vibration;
    }
  }, [state?.controller.vibration]);

  useEffect(() => AppWatcher.listen(setGame), []);

  useEffect(() => {
    const request = ++gameRequest.current;
    if (!game) {
      setPerGame(false);
      void getState().then(next => {
        if (request !== gameRequest.current || AppWatcher.currentGame()) return;
        setState(next); setPreset(detectPreset(next.tdp, next.presets));
      });
      return;
    }
    void getGameProfile(game.appId).then(profile => {
      if (request !== gameRequest.current || AppWatcher.currentGame()?.appId !== game.appId) return;
      setPerGame(profile.exists);
      if (profile.exists) {
        setPreset(detectPreset(profile.profile, state?.presets ?? {}));
        setState(current => current ? { ...current, tdp: profile.profile } : current);
      } else {
        void getState().then(next => {
          if (request !== gameRequest.current || AppWatcher.currentGame()?.appId !== game.appId) return;
          setState(next); setPreset(detectPreset(next.tdp, next.presets));
        });
      }
    }).catch(error => console.error("[ayaneo3companion] game profile lookup failed", error));
    return () => { gameRequest.current += 1; };
  }, [game?.appId]);

  const refresh = useCallback(async () => {
    try {
      const next = await getState();
      setState(current => {
        if (!current) return next;
        return {
          ...next,
          tdp: tdpDirty.current ? current.tdp : next.tdp,
          controller: controllerDirty.current || ffGainDirty.current ? current.controller : next.controller,
        };
      });
      if (!presetInitialized.current) { presetInitialized.current = true; setPreset(detectPreset(next.tdp, next.presets)); }
    } catch { /* backend may still be starting */ }
  }, []);

  useEffect(() => {
    if (visible && !wasVisible.current) {
      setActiveSection(null);
      setStatus(null);
    }
    wasVisible.current = visible;
    refresh();
    if (!visible) return;
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh, visible]);

  useEffect(() => {
    if (!visible || activeSection !== "battery") return;
    let cancelled = false;
    const updateBattery = async () => {
      try {
        const next = await getBatteryStatus();
        if (!cancelled) setBattery(next);
      } catch (error) {
        console.warn("[ayaneo3companion] battery status unavailable", error);
      }
    };
    void updateBattery();
    const timer = setInterval(() => void updateBattery(), 10000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [activeSection, visible]);

  useEffect(() => () => {
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    if (ffGainTimer.current) clearTimeout(ffGainTimer.current);
  }, []);

  const run = async (action: ActionKey, work: () => Promise<State>, title: string, success?: string) => {
    if (pendingActionRef.current) return;
    pendingActionRef.current = action;
    setPendingAction(action); setStatus(null);
    try { setState(await work()); if (success) setStatus(success); }
    catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus(`Error: ${message}`); toaster.toast({ title, body: message }); await refresh();
    } finally { pendingActionRef.current = null; setPendingAction(null); }
  };

  if (!state) return <PanelSection><PanelSectionRow><Spinner /></PanelSectionRow></PanelSection>;
  if (!state.supported) return <PanelSection title="Unsupported device"><PanelSectionRow><Field label={state.device} description="AYANEO 3 is required." /></PanelSectionRow></PanelSection>;

  const tdp = state.tdp;
  const tuning = normalise(fromAbsolute(tdp));
  const spptOff = tuning.spptOff;
  const fpptOff = tuning.fpptOff;
  const maxSpptOffset = spptOffsetMax(tuning.spl);
  const maxFpptOffset = fpptOffsetMax(tuning.spl);
  const setCustomTdp = (next: Tdp) => { tdpDirty.current = true; setPreset("Custom"); setState(current => current ? { ...current, tdp: next } : current); setStatus(null); };
  const setSpl = (value: number) => {
    if (!Number.isFinite(value)) return;
    setCustomTdp(absolute(normalise({ ...tuning, spl: value })));
  };
  const setSpptOff = (value: number) => {
    if (!Number.isFinite(value)) return;
    const nextSppt = clamp(value, 0, maxSpptOffset);
    setCustomTdp(absolute(normalise({ ...tuning, spptOff: nextSppt, fpptOff: Math.max(tuning.fpptOff, nextSppt) })));
  };
  const setFpptOff = (value: number) => {
    if (!Number.isFinite(value)) return;
    const nextFppt = clamp(value, 0, maxFpptOffset);
    setCustomTdp(absolute(normalise({ ...tuning, fpptOff: nextFppt, spptOff: Math.min(tuning.spptOff, nextFppt) })));
  };
  const choosePreset = (name: Preset) => {
    setPreset(name); setStatus(null);
    if (name === "Custom") return;
    tdpDirty.current = false;
    const value = state.presets[name]; setState(current => current ? { ...current, tdp: value } : current);
    void run("tdp", () => perGame && game ? setGameProfile(game.appId, value) : setTdp(value), "TDP preset failed",
      perGame && game ? `${name} saved for ${game.name}.` : `${name} applied.`);
  };
  const togglePerGame = async (enabled: boolean) => {
    if (!game || pendingActionRef.current) return;
    pendingActionRef.current = "profile";
    setPendingAction("profile");
    setPerGame(enabled); setStatus(null);
    try {
      if (enabled) {
        const profile = await getGameProfile(game.appId);
        if (profile.exists) {
          setPreset(detectPreset(profile.profile, state.presets));
          setState(current => current ? { ...current, tdp: profile.profile } : current);
          setStatus(`Using the saved profile for ${game.name}.`);
        } else {
          setStatus(`Choose a preset or Custom settings for ${game.name}.`);
        }
      } else {
        const next = await deleteGameProfile(game.appId);
        setState(next); setPreset(detectPreset(next.tdp, next.presets));
        setStatus(`Global TDP restored for ${game.name}.`);
      }
    } catch (error) {
      setPerGame(!enabled);
      const message = error instanceof Error ? error.message : String(error);
      setStatus(`Error: ${message}`);
      toaster.toast({ title: enabled ? "Game profile lookup failed" : "Game profile removal failed", body: message });
    } finally {
      pendingActionRef.current = null;
      setPendingAction(null);
    }
  };
  const flushController = async () => {
    if (controllerWriting.current) return;
    controllerWriting.current = true;
    try {
      while (controllerPending.current) {
        const next = controllerPending.current;
        controllerPending.current = null;
        const previousVibration = appliedVibration.current ?? state.controller.vibration;
        const vibrationChanged = next.vibration !== previousVibration;
        try {
          const applied = vibrationChanged
            ? await setControllerWithVibrationFeedback(next)
            : await setController(next);
          appliedVibration.current = next.vibration;
          if (!controllerPending.current) {
            controllerDirty.current = false;
            setState(current => current ? { ...applied, tdp: current.tdp, controller: next } : applied);
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          toaster.toast({ title: "Controller setting failed", body: message });
          if (!controllerPending.current) {
            controllerDirty.current = false;
            void refresh();
          }
        }
      }
    } finally {
      controllerWriting.current = false;
      if (controllerPending.current) void flushController();
    }
  };
  const applyController = (part: Partial<Controller>, delay = 0) => {
    const base = controllerPending.current ?? state.controller;
    const controller = { ...base, ...part };
    if (controller.rgb_mode !== "off") lastRgbMode.current = controller.rgb_mode;
    controllerDirty.current = true;
    controllerPending.current = controller;
    setState(current => current ? { ...current, controller } : current);
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    controllerTimer.current = setTimeout(() => { controllerTimer.current = null; void flushController(); }, delay);
  };
  const setVibrationLevel = (value: number) => {
    if (!Number.isFinite(value)) return;
    const vibration = VIBRATION_LEVELS[Math.round(clamp(value, 0, VIBRATION_LEVELS.length - 1))];
    if (vibration !== state.controller.vibration) applyController({ vibration }, VIBRATION_APPLY_DELAY_MS);
  };
  const setFfGain = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const value = Math.round(clamp(raw, 0, 100) / 10) * 10;
    if (value === state.controller.ff_gain && ffGainPending.current === null) return;
    ffGainDirty.current = true;
    ffGainPending.current = value;
    if (controllerPending.current) controllerPending.current = { ...controllerPending.current, ff_gain: value };
    setState(current => current ? { ...current, controller: { ...current.controller, ff_gain: value } } : current);
    if (ffGainTimer.current) clearTimeout(ffGainTimer.current);
    ffGainTimer.current = setTimeout(async () => {
      ffGainTimer.current = null;
      const target = ffGainPending.current;
      if (target === null) return;
      try {
        const applied = await setVibrationGain(target);
        if (ffGainPending.current === target) {
          ffGainPending.current = null;
          ffGainDirty.current = false;
          setState(current => current ? {
            ...applied,
            tdp: current.tdp,
            controller: { ...applied.controller, ff_gain: target },
          } : applied);
        }
      } catch (error) {
        ffGainPending.current = null;
        ffGainDirty.current = false;
        const message = error instanceof Error ? error.message : String(error);
        toaster.toast({ title: "FF gain failed", body: message });
        await refresh();
      }
    }, 150);
  };
  const previewRgb = (next: Hsv) => {
    controllerDirty.current = true;
    setRgbEdit(next);
  };
  const commitRgb = (next: Hsv) => {
    setRgbEdit(next);
    applyController({ color: hsvToHex(next.hue, next.saturation), brightness: next.brightness });
  };
  const runVibrationTest = async () => {
    if (pendingActionRef.current) return;
    pendingActionRef.current = "vibration";
    setPendingAction("vibration");
    try {
      const result = await testVibration(VIBRATION_TEST_MS);
      if (!result.success) toaster.toast({ title: "Vibration test failed", body: result.error ?? "Unknown error" });
    } catch (error) {
      toaster.toast({ title: "Vibration test failed", body: error instanceof Error ? error.message : String(error) });
    } finally { pendingActionRef.current = null; setPendingAction(null); }
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

  if (!activeSection) return <PageShell>
    <PanelSection title="Hardware Controls">
      <SectionLink title="TDP" description={`${preset} · ${tdp.spl} / ${tdp.sppt} / ${tdp.fppt} W`} onClick={() => openSection("tdp")} />
      <SectionLink title="Vibration" description={`${titleCase(state.controller.vibration)} · FF Gain ${state.controller.ff_gain}%`} onClick={() => openSection("vibration")} />
      <SectionLink title="RGB" description={state.controller.rgb_mode === "off" ? "Off" : `${titleCase(state.controller.rgb_mode)} · #${state.controller.color.toUpperCase()}`} onClick={() => openSection("rgb")} />
      <SectionLink title="Battery" description={state.charge_bypass ? "Bypass charging active" : "Automatic charging"} onClick={() => openSection("battery")} />
      <SectionLink title="Magic Modules" description={moduleSummary} onClick={() => openSection("modules")} />
      <SectionLink title="Audio" description={audioSummary} onClick={() => openSection("audio")} />
      <SectionLink title="Key Binding" description={`${state.button_fix_installed ? "L5/R5 enabled" : "Native mapping"} · TM Guard ${state.tm_guard_enabled ? "on" : "off"}`} onClick={() => openSection("buttons")} />
      <SectionLink title="OLED Display" description={state.screen_installed ? `Definition installed · ${state.edid_game_nits || 800} nits` : "Display definition not installed"} onClick={() => openSection("screen")} />
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow><Field label={state.device} description={`TDP backend: ${state.tdp_backend}`} /></PanelSectionRow>
    </PanelSection>
  </PageShell>;

  return <PageShell>
    <SectionHeader title={activeSection === "modules" ? "Magic Modules" : activeSection === "buttons" ? "Key Binding" : activeSection === "screen" ? "OLED Display" : titleCase(activeSection)} onBack={backToControls} />

    {activeSection === "tdp" && <>
      <PanelSection title="Current TDP">
        <PanelSectionRow><Field label={`${tdp.spl} / ${tdp.sppt} / ${tdp.fppt} W`} description={`SPL / SPPT / FPPT · ${state.gpu_power_w == null ? "power unavailable" : `${state.gpu_power_w.toFixed(1)} W currently`} · ${state.tdp_backend}`} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Game Profile">
        <PanelSectionRow><ToggleField label="Per Game Profile" description={game ? game.name : "No game running"} checked={perGame} disabled={!game || busy} onChange={enabled => void togglePerGame(enabled)} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Preset">
        <PanelSectionRow><DropdownItem label="Power Profile" description={perGame && game ? `Saved for ${game.name}` : "Global profile"} selectedOption={preset} rgOptions={presetOptions} disabled={busy} onChange={option => choosePreset(option.data as Preset)} /></PanelSectionRow>
      </PanelSection>
      {preset === "Custom" && <>
        <PanelSection title="TDP Limits">
          <PanelSectionRow><SliderField label={`SPL (TDP) · ${tuning.spl} W`} value={tuning.spl} min={5} max={35} step={1} onChange={setSpl} description="Sustained power limit" /></PanelSectionRow>
          <PanelSectionRow><SliderField key={`sppt-${tuning.spl}-${maxSpptOffset}`} label={`SPPT · ${tuning.spl + spptOff} W`} value={spptOff} min={0} max={maxSpptOffset || 1} step={1} disabled={maxSpptOffset === 0} onChange={setSpptOff} description={maxSpptOffset === 0 ? "No slow-boost headroom remains" : `+${spptOff} W above SPL · maximum 37 W`} /></PanelSectionRow>
          {state.tdp_backend !== "PowerStation"
            ? <PanelSectionRow><SliderField key={`fppt-${tuning.spl}-${maxFpptOffset}`} label={`FPPT · ${tuning.spl + fpptOff} W`} value={fpptOff} min={0} max={maxFpptOffset || 1} step={1} disabled={maxFpptOffset === 0} onChange={setFpptOff} description={maxFpptOffset === 0 ? "No fast-boost headroom remains" : `+${fpptOff} W above SPL · maximum 37 W`} /></PanelSectionRow>
            : <PanelSectionRow><Field label="FPPT managed automatically" description="PowerStation derives the fast limit from SPL and SPPT." /></PanelSectionRow>}
        </PanelSection>
        <PanelSection title="Apply">
          <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => {
            tdpDirty.current = false;
            void run("tdp", () => perGame && game ? setGameProfile(game.appId, tdp) : setTdp(tdp), "TDP apply failed", perGame && game ? `Custom settings saved for ${game.name}.` : "Custom settings applied.");
          }}>{pendingAction === "tdp" ? "Applying..." : perGame && game ? `Apply & Save for ${game.name}` : "Apply TDP"}</ButtonItem></PanelSectionRow>
        </PanelSection>
      </>}
    </>}

    {activeSection === "vibration" && <PanelSection title="Vibration">
      <PanelSectionRow><SliderField label={`Firmware Strength · ${titleCase(state.controller.vibration)}`} description="Off · Low · Medium · High" value={VIBRATION_LEVELS.indexOf(state.controller.vibration)} min={0} max={3} step={1} notchCount={4} notchTicksVisible validValues="steps" minimumDpadGranularity={1} showValue={false} onChange={setVibrationLevel} /></PanelSectionRow>
      <PanelSectionRow><SliderField label="FF Gain" description="Scales force-feedback effects from games and vibration tests." value={state.controller.ff_gain} min={0} max={100} step={10} notchCount={11} notchTicksVisible validValues="steps" minimumDpadGranularity={10} showValue valueSuffix="%" onChange={setFfGain} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem label="Test Vibration" description="Play a short 500 ms rumble pulse." layout="inline" disabled={busy || state.controller.vibration === "off" || state.controller.ff_gain === 0} onClick={() => void runVibrationTest()}>{pendingAction === "vibration" ? <Spinner /> : "Test"}</ButtonItem></PanelSectionRow>
      <PanelSectionRow><Field label="Two-stage control" description="Firmware Strength selects the controller's base level; FF Gain scales Linux force-feedback effects from 0 to 100%." /></PanelSectionRow>
    </PanelSection>}

    {activeSection === "rgb" && <PanelSection title="LED Settings">
      <PanelSectionRow><ToggleField label="Enable LED Control" checked={state.controller.rgb_mode !== "off"} onChange={enabled => applyController({ rgb_mode: enabled ? lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED Mode" selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onChange={option => applyController({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
        <PanelSectionRow><Field label={`#${state.controller.color.toUpperCase()}`} description={`${titleCase(state.controller.rgb_mode)} · ${rgbEdit.brightness}% brightness`}><span style={{ display: "block", width: "28px", height: "28px", borderRadius: "50%", background: `#${state.controller.color}`, border: "2px solid rgba(255,255,255,.55)" }} /></Field></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Hue" value={rgbEdit.hue} min={0} max={359} valueSuffix="°" className="AyaneoRgbHue" onChange={hue => previewRgb({ ...rgbEdit, hue })} onChangeEnd={hue => commitRgb({ ...rgbEdit, hue })} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Saturation" value={rgbEdit.saturation} min={0} max={100} valueSuffix="%" className="AyaneoRgbSaturation" onChange={saturation => previewRgb({ ...rgbEdit, saturation })} onChangeEnd={saturation => commitRgb({ ...rgbEdit, saturation })} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Brightness" value={rgbEdit.brightness} min={0} max={100} valueSuffix="%" className="AyaneoRgbBrightness" onChange={brightness => previewRgb({ ...rgbEdit, brightness })} onChangeEnd={brightness => commitRgb({ ...rgbEdit, brightness })} /></PanelSectionRow>
        <style>{`
          .AyaneoRgbHue .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, hsl(0,100%,50%), hsl(60,100%,50%), hsl(120,100%,50%), hsl(180,100%,50%), hsl(240,100%,50%), hsl(300,100%,50%), hsl(360,100%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
          .AyaneoRgbSaturation .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, hsl(${rgbEdit.hue},0%,100%), hsl(${rgbEdit.hue},100%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
          .AyaneoRgbBrightness .${gamepadSliderClasses.SliderTrack} { background: linear-gradient(to right, #000, hsl(${rgbEdit.hue},${rgbEdit.saturation}%,50%)) !important; --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important; }
        `}</style>
      </>}
    </PanelSection>}

    {activeSection === "battery" && <>
      <PanelSection title="Battery Status">
        {!battery
          ? <PanelSectionRow><Spinner /></PanelSectionRow>
          : <PanelSectionRow><Field
              label={battery.available ? `${battery.percent == null ? "Battery" : `${battery.percent}%`} · ${battery.status}` : "Battery unavailable"}
              description={battery.seconds_to_full != null
                ? `Estimated time to full: ${formatDuration(battery.seconds_to_full)}${battery.power_w == null ? "" : ` · ${battery.power_w.toFixed(1)} W net charge`}`
                : battery.status.toLowerCase() === "charging" ? "Calculating the charging estimate..."
                : battery.status.toLowerCase() === "full" ? "The battery is fully charged."
                : "Time to full is shown while the battery is charging."}
            /></PanelSectionRow>}
      </PanelSection>
      <PanelSection title="Charge Control">
        <PanelSectionRow><ToggleField label="Bypass Charging" description={state.charge_bypass_supported ? "Power the console from the charger without charging the battery." : "Charge bypass is unavailable on this kernel."} checked={state.charge_bypass} disabled={busy || !state.charge_bypass_supported} onChange={enabled => void run("battery", () => setChargeBypass(enabled), "Charge bypass failed", enabled ? "Charging bypass enabled." : "Automatic charging restored.")} /></PanelSectionRow>
        <PanelSectionRow><Field label={state.charge_bypass ? "Bypass active" : "Automatic charging"} description="State read directly from the AYANEO embedded controller." /></PanelSectionRow>
      </PanelSection>
    </>}

    {activeSection === "modules" && <>
      <PanelSection title="Installed Modules">
        <PanelSectionRow><Field label={`Left · ${state.module_left.label}`} description={[state.module_left.layout, state.module_left.code == null ? "" : `Module ID 0x${state.module_left.code.toString(16).toUpperCase().padStart(2, "0")}`].filter(Boolean).join(" · ")} /></PanelSectionRow>
        <PanelSectionRow><Field label={`Right · ${state.module_right.label}`} description={[state.module_right.layout, state.module_right.code == null ? "" : `Module ID 0x${state.module_right.code.toString(16).toUpperCase().padStart(2, "0")}`].filter(Boolean).join(" · ")} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Controller Eject">
        <PanelSectionRow><Field label={state.modules_reconnecting ? "Waiting for both modules" : state.modules_connected ? "Both modules connected" : "Module disconnected"} description="After ejection, insert both modules to initialise the controller again." /></PanelSectionRow>
        {(["left", "right", "both"] as const).map(side => <PanelSectionRow key={side}><ButtonItem label={`Eject ${titleCase(side)}`} description={side === "both" ? "Release both controller modules." : `Release the ${side} controller module.`} layout="inline" disabled={busy || state.modules_reconnecting || !state.module_eject_supported} onClick={() => showModal(<ConfirmModal strTitle={`Eject ${titleCase(side)}`} strDescription={`Release ${side === "both" ? "both controller modules" : `the ${side} controller module`}?`} strOKButtonText="Eject" bDestructiveWarning onOK={() => void run("modules", () => ejectModules(side), "Module eject failed", `${titleCase(side)} module${side === "both" ? "s" : ""} released.`)} />)}>{pendingAction === "modules" ? <Spinner /> : "Eject"}</ButtonItem></PanelSectionRow>)}
      </PanelSection>
      <PanelSection title="Recovery">
        <PanelSectionRow><ButtonItem label="Reset Magic Modules" description="Reinitialise both modules and restore RGB, vibration, FF Gain and rear-button mappings." layout="inline" disabled={busy || !state.modules_connected || !state.module_reset_supported} onClick={() => showModal(<ConfirmModal strTitle="Reset Magic Modules" strDescription="The controller will briefly disconnect while both modules are reinitialised." strOKButtonText="Reset" onOK={() => void run("modules", () => resetModules(), "Magic Module reset failed", "Both modules reset and detected.")} />)}>{pendingAction === "modules" ? <Spinner /> : "Reset"}</ButtonItem></PanelSectionRow>
      </PanelSection>
    </>}

    {activeSection === "audio" && <>
      <PanelSection title="Smart Amp Tuning">
        <PanelSectionRow><ToggleField label="AYANEO Speaker Tuning" description={state.audio_fix_supported ? "Load the official AYANEO 3 profile for both CS35L41 smart amplifiers." : "The required SteamOS firmware or CS35L41 controls are unavailable."} checked={state.audio_fix_enabled} disabled={busy || !state.audio_fix_supported} onChange={enabled => void run("audio", () => setAudioFix(enabled), "Audio fix failed", enabled ? "AYANEO speaker tuning enabled." : "Generic audio profile restored.")} /></PanelSectionRow>
        <PanelSectionRow><Field label={state.audio_fix_installed ? state.audio_profile : state.audio_profile === "Pending" ? "Applying..." : "Not applied"} description={state.audio_fix_error || (state.audio_fix_installed ? "Both speaker DSPs use the device-specific v0.65 tuning." : "SteamOS is using the generic Cirrus speaker profile.")} /></PanelSectionRow>
        <PanelSectionRow><ButtonItem label="Reapply Audio Fix" description="Reload the tuning if audio was reset by the system." layout="inline" disabled={busy || !state.audio_fix_enabled || !state.audio_fix_supported} onClick={() => void run("audio", () => reapplyAudioFix(), "Audio fix failed", "AYANEO speaker tuning reapplied.")}>{pendingAction === "audio" ? <Spinner /> : "Apply"}</ButtonItem></PanelSectionRow>
      </PanelSection>
      {state.audio_calibration_available && <PanelSection title="Speaker Calibration">
        <PanelSectionRow><Field label={state.audio_calibration_last || "Factory calibration active"} description="Measures both speakers, validates the result and saves it to EFI. The previous calibration is backed up first." /></PanelSectionRow>
        <PanelSectionRow><ButtonItem label="Recalibrate Audio" description="Requires a quiet room and a clear surface." layout="inline" disabled={busy} onClick={() => showModal(<ConfirmModal strTitle="Recalibrate Speakers" strDescription="Place the console on a clear surface in a quiet room. A calibration signal will play and the result will be written to EFI." strOKButtonText="Recalibrate" bDestructiveWarning onOK={() => void run("audio", () => recalibrateAudio(), "Audio recalibration failed", "Calibration saved. Restart the console to apply it.")} />)}>{pendingAction === "audio" ? <Spinner /> : "Start"}</ButtonItem></PanelSectionRow>
      </PanelSection>}
    </>}

    {activeSection === "buttons" && <PanelSection title="Controller Buttons">
      <PanelSectionRow><ToggleField label="Fix Key Binding" description="Program LC1/RC1 as L5/R5 while preserving native LC/RC as L4/R4. Menu/QAM variants are detected automatically." checked={state.button_fix_installed} disabled={busy} onChange={enabled => void run("buttons", () => setButtonFix(enabled), "Key binding fix failed", enabled ? "Key binding fix enabled." : "Native key mapping restored.")} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="TM Guard" description="Return the controller to custom gamepad mode after an accidental TM press." checked={state.tm_guard_enabled} disabled={busy} onChange={enabled => void run("buttons", () => setTmGuard(enabled), "TM Guard failed", enabled ? "TM Guard enabled." : "TM Guard disabled.")} /></PanelSectionRow>
      <PanelSectionRow><Field label={state.tm_guard_status} description={state.tm_guard_recoveries ? `Recovered ${state.tm_guard_recoveries} TM mode change${state.tm_guard_recoveries === 1 ? "" : "s"} since plugin start.` : "The physical TM button remains available when TM Guard is disabled."} /></PanelSectionRow>
    </PanelSection>}

    {activeSection === "screen" && <PanelSection title="Display Definition">
      <PanelSectionRow><ToggleField label="Install Display Definition" description="Install the AYANEO 3 OLED HDR, colour and 60/90/120/144 Hz gamescope definition with Gamma 2.2 output." checked={state.screen_installed} disabled={busy} onChange={enabled => void run("screen", () => setScreenFix(enabled), "Display fix failed", enabled ? "Display definition installed. Restart Game Mode." : "Display definition removed. Restart Game Mode.")} /></PanelSectionRow>
      <PanelSectionRow><Field label={state.edid_patched ? "EDID metadata normalized" : "EDID update waiting"} description={state.edid_game_nits ? `Games see AYANEO's advertised ${state.edid_game_nits}-nit maximum.` : "Waiting for gamescope to publish the display EDID."} /></PanelSectionRow>
      <PanelSectionRow><Field label="Restart required after changing" description="Restart Game Mode manually so gamescope reloads the display definition." /></PanelSectionRow>
    </PanelSection>}

    {status && <PanelSection title="Status"><PanelSectionRow><Field label={status.startsWith("Error:") ? "Action failed" : "Done"} description={status.replace(/^Error:\s*/, "")} /></PanelSectionRow></PanelSection>}
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
    icon: <Icon />,
    onDismount() { AppWatcher.stop(); },
  };
});
