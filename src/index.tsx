// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, ConfirmModal, DropdownItem, Field, gamepadDialogClasses, gamepadSliderClasses, PanelSection, PanelSectionRow, Router, showModal, SliderField, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, ReactElement, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { FaChevronRight } from "react-icons/fa";

type Vibration = "off" | "low" | "medium" | "high";
type RgbMode = "off" | "solid" | "pulse" | "rainbow";
type Preset = "Minimum" | "Low power" | "Balanced" | "Performance" | "Max" | "Custom";
type SectionKey = "tdp" | "vibration" | "rgb" | "battery" | "modules" | "audio" | "buttons" | "screen" | "about";
type ActionKey = "tdp" | "profile" | "cpu_boost" | "vibration" | "battery" | "modules" | "audio" | "buttons" | "screen";
interface Tdp { spl: number; sppt: number; fppt: number }
interface Tuning { spl: number; spptOff: number; fpptOff: number }
interface Controller { vibration: Vibration; ff_gain: number; rgb_mode: RgbMode; color: string; brightness: number }
interface Hsv { hue: number; saturation: number; brightness: number }
interface RunningGame { appId: string; name: string }
interface GameProfile { exists: boolean; profile: Tdp; preset?: Preset }
interface ModuleInfo { code: number | null; label: string; layout: string; status: string; connected: boolean }
interface BatteryStatus { available: boolean; percent: number | null; status: string; seconds_to_full: number | null; power_w: number | null; source: "UPower" | "sysfs" | "none" }
interface UpdateInfo { current_version?: string; latest_version?: string; update_available?: boolean; download_url?: string | null; asset_name?: string | null; error?: string }
interface State { settings_error?: string; initializing?: boolean; startup_error?: string; supported: boolean; device: string; version: string; tdp_backend: string; tdp: Tdp; tdp_preset?: Preset; presets: Record<string, Tdp>; cpu_boost_supported: boolean; cpu_boost: boolean; controller: Controller; gpu_power_w: number | null; screen_installed: boolean; screen_conflict: boolean; edid_patched: boolean; edid_game_nits: number; button_fix_installed: boolean; charge_bypass_supported: boolean; charge_bypass: boolean; module_eject_supported: boolean; module_reset_supported: boolean; modules_reconnecting: boolean; modules_connected: boolean; module_left: ModuleInfo; module_right: ModuleInfo; tm_guard_enabled: boolean; tm_guard_status: string; tm_guard_recoveries: number; audio_fix_supported: boolean; audio_fix_enabled: boolean; audio_fix_installed: boolean; audio_profile: string; audio_fix_error: string; audio_calibration_available: boolean; audio_calibration_last: string }

const getState = callable<[], State>("get_state");
const getBatteryStatus = callable<[], BatteryStatus>("get_battery_status");
const getVersion = callable<[], { version: string }>("get_version");
const checkForUpdates = callable<[], UpdateInfo>("check_for_updates");
const performUpdate = callable<[], { success: boolean; path?: string; error?: string }>("perform_update");
const setTdp = callable<[Tdp, Preset, string], State>("set_tdp");
const setCpuBoost = callable<[boolean], State>("set_cpu_boost");
const getGameProfile = callable<[string], GameProfile>("get_game_profile");
const setGameProfile = callable<[string, Tdp, Preset, string], State>("set_game_profile");
const deleteGameProfile = callable<[string, string], State>("delete_game_profile");
const setActiveApp = callable<[string], void>("set_active_app");
const setController = callable<[Controller], State>("set_controller");
const setControllerWithVibrationFeedback = callable<[Controller], State>("set_controller_with_vibration_feedback");
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
const formatDuration = (seconds: number): string => {
  const minutes = Math.max(1, Math.round(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  if (!hours) return `${minutes} min`;
  return remainder ? `${hours} hr ${remainder} min` : `${hours} hr`;
};
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
    if (this.lifetimeTimer) clearTimeout(this.lifetimeTimer);
    this.lifetimeTimer = undefined;
    if (this.timer) clearInterval(this.timer);
    for (const unsubscribe of this.unsubs) {
      try { unsubscribe(); } catch { /* subscription may already be gone */ }
    }
    this.timer = undefined; this.unsubs = []; this.listeners = []; this.current = null; this.started = false; this.lastPush = 0;
  }
  private static async check(force = false) {
    if (!this.started || this.busy) return;
    const generation = this.generation;
    const game = this.activeGame();
    const changed = game?.appId !== this.current?.appId;
    this.current = game;
    if (changed) this.listeners.forEach(listener => listener(game));
    const now = Date.now();
    if (force || changed || now - this.lastPush >= 6000) {
      this.busy = true;
      try { await setActiveApp(game?.appId ?? ""); if (generation === this.generation) this.lastPush = now; }
      catch (error) { console.error("[ayaneo3companion] active app update failed", error); }
      finally { if (generation === this.generation) this.busy = false; }
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
  onChange(value: number): void; onChangeEnd(value: number): void;
}

// The parent owns the debounce and write queue, so leaving the RGB page cannot
// discard a pending slider value or send an older sibling slider closure.
const SlowSliderField: FC<SlowSliderProps> = slider => <SliderField
  label={slider.label} value={slider.value} min={slider.min} max={slider.max}
  validValues="range" showValue valueSuffix={slider.valueSuffix} className={slider.className}
  onChange={value => { slider.onChange(value); slider.onChangeEnd(value); }}
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

const StackedAction: FC<{
  title: string; description: string; disabled?: boolean; onClick: () => void; children: ReactNode;
}> = ({ title, description, disabled, onClick, children }) => (
  <PanelSectionRow>
    <div style={{ width: "100%", maxWidth: "100%", minWidth: 0, boxSizing: "border-box" }}>
      <div className={gamepadDialogClasses.FieldLabel} style={{ marginBottom: "6px" }}>{title}</div>
      <div className={gamepadDialogClasses.FieldDescription} style={{ marginBottom: "8px", overflowWrap: "anywhere" }}>{description}</div>
      <div style={{ width: "100%", maxWidth: "100%", minWidth: 0 }}>
        <ButtonItem layout="below" bottomSeparator="none" disabled={disabled} onClick={onClick}>{children}</ButtonItem>
      </div>
    </div>
  </PanelSectionRow>
);

interface UpdateView { info: UpdateInfo | null; checking: boolean; downloading: boolean; path: string | null }
let updateView: UpdateView = { info: null, checking: false, downloading: false, path: null };
let updateGeneration = 0;
const updateListeners = new Set<(value: UpdateView) => void>();
const publishUpdate = (next: Partial<UpdateView>) => {
  updateView = { ...updateView, ...next };
  updateListeners.forEach(listener => listener(updateView));
};
const UpdateSection: FC<{ initialVersion: string }> = ({ initialVersion }) => {
  const [view, setView] = useState(updateView);
  const { info: updateInfo, checking, downloading, path: downloadPath } = view;
  const [version, setVersion] = useState(initialVersion);

  useEffect(() => {
    let active = true;
    updateListeners.add(setView);
    setView(updateView);
    getVersion()
      .then(result => { if (active && result.version) setVersion(result.version); })
      .catch(() => undefined);
    return () => { active = false; updateListeners.delete(setView); };
  }, []);

  const notifyFailure = (title: string, error: unknown) => {
    const body = error instanceof Error ? error.message : String(error ?? "Unknown error");
    try { toaster.toast({ title, body, duration: 4000 }); }
    catch { console.error(`[ayaneo3companion] ${title}: ${body}`); }
  };

  const check = useCallback(async () => {
    if (updateView.checking || updateView.downloading) return;
    const generation = updateGeneration;
    publishUpdate({ checking: true, info: null, path: null });
    try {
      const info = await checkForUpdates();
      if (generation === updateGeneration) publishUpdate({ info });
    } catch (error) {
      if (generation === updateGeneration) {
        notifyFailure("Update check failed", error);
        publishUpdate({ info: { error: error instanceof Error ? error.message : String(error) } });
      }
    } finally {
      if (generation === updateGeneration) publishUpdate({ checking: false });
    }
  }, []);

  const download = useCallback(async () => {
    if (updateView.checking || updateView.downloading) return;
    const generation = updateGeneration;
    publishUpdate({ downloading: true });
    try {
      const result = await performUpdate();
      if (generation !== updateGeneration) return;
      if (result.success && result.path) publishUpdate({ path: result.path });
      else throw new Error(result.error ?? "Unknown error");
    } catch (error) {
      if (generation === updateGeneration) {
        publishUpdate({ info: { ...(updateView.info ?? {}), error: error instanceof Error ? error.message : String(error) } });
        notifyFailure("Download failed", error);
      }
    } finally {
      if (generation === updateGeneration) publishUpdate({ downloading: false });
    }
  }, []);

  const versionTag = (value: string) => (
    <span style={{ fontWeight: 700, color: "var(--gpColor-White, #fff)", background: "rgba(102,192,244,.18)", borderRadius: "4px", padding: "1px 6px" }}>
      v{value}
    </span>
  );

  return <PanelSection title="Updates">
    <PanelSectionRow>
      <div style={{ width: "100%", minWidth: 0, fontSize: "12px", color: "var(--gpColor-TextMuted, rgba(255,255,255,.6))", overflowWrap: "anywhere" }}>
        Installed: {versionTag((updateInfo?.current_version ?? version) || "?")}
        {updateInfo?.latest_version && !updateInfo.error && <>
          {" · "}Latest: {versionTag(updateInfo.latest_version)}
        </>}
      </div>
    </PanelSectionRow>
    {updateInfo?.error && <PanelSectionRow>
      <div style={{ width: "100%", minWidth: 0, padding: "8px", boxSizing: "border-box", borderRadius: "4px", background: "rgba(248,113,113,.14)", color: "var(--gpColor-Red, #f87171)", overflowWrap: "anywhere" }}>
        {updateInfo.error}
      </div>
    </PanelSectionRow>}
    {updateInfo && !updateInfo.error && !updateInfo.update_available && !downloadPath && <PanelSectionRow>
      <div style={{ fontSize: "12px", color: "var(--gpColor-Green, #4ade80)" }}>Up to date</div>
    </PanelSectionRow>}
    {updateInfo?.update_available && updateInfo.download_url && updateInfo.asset_name && !downloadPath && <StackedAction
      title={`Version ${updateInfo.latest_version} is available`}
      description="Download the exact GitHub release ZIP to the SteamOS Downloads folder."
      disabled={downloading || checking}
      onClick={() => void download()}
    >{downloading ? <Spinner /> : `Download v${updateInfo.latest_version}`}</StackedAction>}
    {downloadPath && <PanelSectionRow>
      <Field
        label="Update downloaded"
        description={<span>Saved to <span style={{ fontFamily: "monospace", overflowWrap: "anywhere" }}>{downloadPath}</span><br /><br />To install: Decky → Settings → Developer → Uninstall AYANEO 3 Companion → Install Plugin from ZIP. Settings and per-game profiles are preserved.</span>}
      />
    </PanelSectionRow>}
    <StackedAction
      title="Check for Updates"
      description="Compare the installed version with the latest GitHub release."
      disabled={checking || downloading}
      onClick={() => void check()}
    >{checking ? <Spinner /> : "Check"}</StackedAction>
  </PanelSection>;
};

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
  const controllerDirty = useRef(false);
  const controllerPending = useRef<Controller | null>(null);
  const controllerWriting = useRef(false);
  const controllerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const appliedController = useRef<Controller | null>(null);
  const lastRgbMode = useRef<RgbMode>("solid");
  const [state, setState] = useState<State | null>(confirmedState);
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [preset, setPreset] = useState<Preset>(() => confirmedState ? confirmedState.tdp_preset ?? detectPreset(confirmedState.tdp, confirmedState.presets) : "Custom");
  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null);
  const pendingActionRef = useRef<ActionKey | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [game, setGame] = useState<RunningGame | null>(AppWatcher.currentGame());
  const [perGame, setPerGame] = useState(false);
  const [profileLoading, setProfileLoading] = useState(Boolean(game));
  const [savedGamePreset, setSavedGamePreset] = useState<Preset | undefined>(undefined);
  const [battery, setBattery] = useState<BatteryStatus | null>(null);
  const gameRequest = useRef(0);
  const profileReady = useRef(!game);
  const contextStateReady = useRef(false);
  const busy = pendingAction !== null || Boolean(state?.settings_error || state?.initializing);
  const tdpBusy = busy || profileLoading;
  const currentGameId = useRef(game?.appId ?? "");
  currentGameId.current = game?.appId ?? "";
  const validContext = (context: string) => mounted.current && currentGameId.current === context && (AppWatcher.activeGame()?.appId ?? "") === context;
  const notifyFailure = (title: string, error: unknown) => {
    const body = error instanceof Error ? error.message : String(error);
    try { toaster.toast({ title, body }); } catch { console.error(`[ayaneo3companion] ${title}: ${body}`); }
  };
  const acceptState = (next: State) => {
    if (!mounted.current) return;
    confirmedState = next;
    setState(current => current ? { ...next,
      tdp: tdpDirty.current ? current.tdp : next.tdp,
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
      appliedController.current = state.controller;
      desiredController.current = state.controller;
      if (state.controller.rgb_mode !== "off") lastRgbMode.current = state.controller.rgb_mode;
    }
  }, [state?.controller]);

  useEffect(() => AppWatcher.listen(next => {
    ++revision.current;
    tdpDirty.current = false;
    presetInitialized.current = false;
    currentGameId.current = next?.appId ?? "";
    profileReady.current = !next;
    contextStateReady.current = false;
    setGame(next);
    setProfileLoading(Boolean(next));
  }), []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !visibleRef.current || reading.current || pendingActionRef.current || controllerWriting.current || controllerPending.current) return;
    reading.current = true;
    const request = revision.current;
    try {
      const next = await getState();
      if (!mounted.current || !visibleRef.current || request !== revision.current) return;
      acceptState(next);
      contextStateReady.current = true;
      setProfileLoading(!profileReady.current);
      if (!presetInitialized.current && !tdpDirty.current && !pendingActionRef.current) {
        presetInitialized.current = true;
        setPreset(next.tdp_preset ?? detectPreset(next.tdp, next.presets));
      }
    } catch (error) {
      if (mounted.current && visibleRef.current && request === revision.current && !confirmedState) {
        setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
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
    setPerGame(false);
    setSavedGamePreset(undefined);
    if (!visible || !game) { if (!game) setProfileLoading(false); return; }
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
        setPerGame(profile.exists);
        if (profile.exists) {
          const stored = profile.preset ?? detectPreset(profile.profile, confirmedState?.presets ?? {});
          setSavedGamePreset(stored);
          setPreset(stored);
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
    ++revision.current;
    if (action === "tdp" || action === "profile") ++gameRequest.current;
    pendingActionRef.current = action;
    setPendingAction(action); setStatus(null);
    try {
      const next = await work();
      if (validContext(context)) { acceptState(next); if (success) setStatus(success); }
    } catch (error) {
      notifyFailure(title, error);
      if (validContext(context)) setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
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
  const toggleCpuBoost = (enabled: boolean) => run("cpu_boost", () => setCpuBoost(enabled), "CPU Boost failed");
  const choosePreset = async (name: Preset) => {
    const context = game?.appId ?? "";
    if (tdpBusy || pendingActionRef.current || !validContext(context)) return;
    const previousPreset = preset;
    const previousTdp = tdp;
    const previousSavedPreset = savedGamePreset;
    presetInitialized.current = true;
    setPreset(name); setStatus(null);
    if (name === "Custom") return;
    const value = state.presets[name];
    if (!value) return;
    ++revision.current;
    ++gameRequest.current;
    tdpDirty.current = false;
    setState(current => current ? { ...current, tdp: value } : current);
    pendingActionRef.current = "tdp";
    setPendingAction("tdp");
    try {
      const next = await (perGame && game
        ? setGameProfile(game.appId, value, name, context)
        : setTdp(value, name, context));
      if (!validContext(context)) return;
      acceptState(next);
      if (perGame && game) setSavedGamePreset(name);
      setStatus(perGame && game ? `${name} saved for ${game.name}.` : `${name} applied.`);
    } catch (error) {
      notifyFailure("TDP preset failed", error);
      if (validContext(context)) {
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
        acceptState(next); setPreset(next.tdp_preset ?? detectPreset(next.tdp, next.presets));
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
  const flushController = async () => {
    if (controllerWriting.current) return;
    controllerWriting.current = true;
    let needsRefresh = false;
    ++revision.current;
    try {
      while (controllerPending.current) {
        const next = controllerPending.current;
        controllerPending.current = null;
        const previous = appliedController.current ?? state.controller;
        const vibrationChanged = next.vibration !== previous.vibration;
        const firmwareChanged = vibrationChanged
          || next.rgb_mode !== previous.rgb_mode
          || next.color !== previous.color
          || next.brightness !== previous.brightness;
        const gainChanged = next.ff_gain !== previous.ff_gain;
        try {
          let applied: State | null = null;
          if (firmwareChanged) {
            applied = vibrationChanged
              ? await setControllerWithVibrationFeedback(next)
              : await setController(next);
          }
          // FF_GAIN lives on a separate evdev device. Keep it in the same
          // serialized queue so an RGB write cannot persist an older gain.
          if (gainChanged) applied = await setVibrationGain(next.ff_gain);
          appliedController.current = applied?.controller ?? next;
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
          appliedController.current = null;
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
      if (controllerPending.current) void flushController();
      else if (needsRefresh && mounted.current) void refresh();
    }
  };
  const applyController = (part: Partial<Controller>, delay = 0) => {
    if (state.settings_error || state.initializing) return;
    ++revision.current;
    const base = controllerPending.current ?? desiredController.current ?? state.controller;
    const controller = { ...base, ...part };
    if (controller.rgb_mode !== "off") lastRgbMode.current = controller.rgb_mode;
    controllerDirty.current = true;
    controllerPending.current = controller;
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
    if (vibration !== state.controller.vibration) applyController({ vibration }, VIBRATION_APPLY_DELAY_MS);
  };
  const setFfGain = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const value = Math.round(clamp(raw, 0, 100) / 10) * 10;
    if (value !== state.controller.ff_gain) applyController({ ff_gain: value }, 150);
  };
  const previewRgb = (next: Hsv) => {
    controllerDirty.current = true;
    desiredRgb.current = next;
    setRgbEdit(next);
  };
  const commitRgb = (next: Hsv) => {
    desiredRgb.current = next;
    setRgbEdit(next);
    applyController({ color: hsvToHex(next.hue, next.saturation), brightness: next.brightness }, 500);
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
    } finally { pendingActionRef.current = null; if (mounted.current) setPendingAction(null); }
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
      <SectionLink title="OLED Display" description={state.screen_installed ? `Definition installed · ${state.edid_game_nits || 800} nits` : state.screen_conflict ? "Existing definition requires confirmation" : "Display definition not installed"} onClick={() => openSection("screen")} />
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow><Field label={state.device} description={`TDP backend: ${state.tdp_backend}`} /></PanelSectionRow>
    </PanelSection>
    <PanelSection title="Plugin">
      <SectionLink title="About" description={`AYANEO 3 Companion · v${state.version}`} onClick={() => openSection("about")} />
    </PanelSection>
  </PageShell>;

  return <PageShell>
    <SectionHeader title={activeSection === "modules" ? "Magic Modules" : activeSection === "buttons" ? "Key Binding" : activeSection === "screen" ? "OLED Display" : titleCase(activeSection)} onBack={backToControls} />

    {activeSection === "tdp" && <>
      <PanelSection title="Current TDP">
        <PanelSectionRow><Field label={`${tdp.spl} / ${tdp.sppt} / ${tdp.fppt} W`} description={`SPL / SPPT / FPPT · ${state.gpu_power_w == null ? "power unavailable" : `${state.gpu_power_w.toFixed(1)} W currently`} · ${state.tdp_backend}`} /></PanelSectionRow>
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
          {state.tdp_backend !== "PowerStation"
            ? <PanelSectionRow><SliderField key={`fppt-${tuning.spl}-${maxFpptOffset}`} label={`FPPT +${fpptOff} W  =  ${tuning.spl + fpptOff} W`} value={fpptOff} min={0} max={maxFpptOffset || 1} step={1} disabled={tdpBusy || maxFpptOffset === 0} onChange={setFpptOff} description={maxFpptOffset === 0 ? "No headroom left at this SPL" : `Fast limit headroom above SPL (max +${maxFpptOffset} W here)`} /></PanelSectionRow>
            : <PanelSectionRow><Field label="FPPT managed automatically" description="PowerStation derives the fast limit from SPL and SPPT." /></PanelSectionRow>}
        </PanelSection>
        <PanelSection title="Action">
          <PanelSectionRow><ButtonItem layout="below" disabled={tdpBusy} onClick={() => {
            const context = game?.appId ?? "";
            if (tdpBusy || !validContext(context)) return;
            void run("tdp", async () => {
              const next = await (perGame && game
                ? setGameProfile(game.appId, tdp, "Custom", context)
                : setTdp(tdp, "Custom", context));
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

    {activeSection === "vibration" && <PanelSection title="Vibration">
      <PanelSectionRow><SliderField label={`Firmware Strength · ${titleCase(state.controller.vibration)}`} description="Off · Low · Medium · High" value={VIBRATION_LEVELS.indexOf(state.controller.vibration)} min={0} max={3} step={1} notchCount={4} notchTicksVisible validValues="steps" minimumDpadGranularity={1} showValue={false} onChange={setVibrationLevel} /></PanelSectionRow>
      <PanelSectionRow><SliderField label="FF Gain" description="Scales force-feedback effects from games and vibration tests." value={state.controller.ff_gain} min={0} max={100} step={10} notchCount={11} notchTicksVisible validValues="steps" minimumDpadGranularity={10} showValue valueSuffix="%" onChange={setFfGain} /></PanelSectionRow>
      <StackedAction title="Test Vibration" description="Play a short 500 ms rumble pulse." disabled={busy || state.controller.vibration === "off" || state.controller.ff_gain === 0} onClick={() => void runVibrationTest()}>{pendingAction === "vibration" ? <Spinner /> : "Test"}</StackedAction>
      <PanelSectionRow><Field label="Two-stage control" description="Firmware Strength selects the controller's base level; FF Gain scales Linux force-feedback effects from 0 to 100%." /></PanelSectionRow>
    </PanelSection>}

    {activeSection === "rgb" && <PanelSection title="LED Settings">
      <PanelSectionRow><ToggleField label="Enable LED Control" checked={state.controller.rgb_mode !== "off"} onChange={enabled => applyController({ rgb_mode: enabled ? lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED Mode" selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onMenuWillOpen={openDropdown} onChange={option => applyController({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
        <PanelSectionRow><Field label={`#${state.controller.color.toUpperCase()}`} description={`${titleCase(state.controller.rgb_mode)} · ${rgbEdit.brightness}% brightness`}><span style={{ display: "block", width: "28px", height: "28px", borderRadius: "50%", background: `#${state.controller.color}`, border: "2px solid rgba(255,255,255,.55)" }} /></Field></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Hue" value={rgbEdit.hue} min={0} max={359} valueSuffix="°" className="AyaneoRgbHue" onChange={hue => previewRgb({ ...desiredRgb.current, hue })} onChangeEnd={hue => commitRgb({ ...desiredRgb.current, hue })} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Saturation" value={rgbEdit.saturation} min={0} max={100} valueSuffix="%" className="AyaneoRgbSaturation" onChange={saturation => previewRgb({ ...desiredRgb.current, saturation })} onChangeEnd={saturation => commitRgb({ ...desiredRgb.current, saturation })} /></PanelSectionRow>
        <PanelSectionRow><SlowSliderField label="Brightness" value={rgbEdit.brightness} min={0} max={100} valueSuffix="%" className="AyaneoRgbBrightness" onChange={brightness => previewRgb({ ...desiredRgb.current, brightness })} onChangeEnd={brightness => commitRgb({ ...desiredRgb.current, brightness })} /></PanelSectionRow>
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
        <PanelSectionRow><ToggleField label="Bypass Charging" description={state.charge_bypass_supported ? "Power the console from the charger without charging the battery." : "AYANEO charge control is not exposed by the current system."} checked={state.charge_bypass} disabled={busy || !state.charge_bypass_supported} onChange={enabled => void run("battery", () => setChargeBypass(enabled), "Charge bypass failed", enabled ? "Charging bypass enabled." : "Automatic charging restored.")} /></PanelSectionRow>
        <PanelSectionRow><Field label={state.charge_bypass ? "Bypass active" : "Automatic charging"} description="State verified through the AYANEO kernel or embedded-controller interface." /></PanelSectionRow>
      </PanelSection>
    </>}

    {activeSection === "modules" && <>
      <PanelSection title="Installed Modules">
        <PanelSectionRow><Field label={`Left · ${state.module_left.label}`} description={[state.module_left.layout, state.module_left.code == null ? "" : `Module ID 0x${state.module_left.code.toString(16).toUpperCase().padStart(2, "0")}`].filter(Boolean).join(" · ")} /></PanelSectionRow>
        <PanelSectionRow><Field label={`Right · ${state.module_right.label}`} description={[state.module_right.layout, state.module_right.code == null ? "" : `Module ID 0x${state.module_right.code.toString(16).toUpperCase().padStart(2, "0")}`].filter(Boolean).join(" · ")} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Controller Eject">
        <PanelSectionRow><Field label={state.modules_reconnecting ? "Waiting for both modules" : state.modules_connected ? "Both modules connected" : "Module disconnected"} description="After ejection, insert both modules to initialise the controller again." /></PanelSectionRow>
        {(["left", "right", "both"] as const).map(side => <StackedAction key={side} title={`Eject ${titleCase(side)}`} description={side === "both" ? "Release both controller modules." : `Release the ${side} controller module.`} disabled={busy || state.modules_reconnecting || !state.module_eject_supported} onClick={() => openModal(<ConfirmModal strTitle={`Eject ${titleCase(side)}`} strDescription={`Release ${side === "both" ? "both controller modules" : `the ${side} controller module`}?`} strOKButtonText="Eject" bDestructiveWarning onOK={() => void run("modules", () => ejectModules(side), "Module eject failed", `${titleCase(side)} module${side === "both" ? "s" : ""} released.`)} />)}>{pendingAction === "modules" ? <Spinner /> : "Eject"}</StackedAction>)}
      </PanelSection>
      <PanelSection title="Recovery">
        <StackedAction title="Reset Magic Modules" description="Reinitialise both modules and restore RGB, vibration, FF Gain and rear-button mappings." disabled={busy || !state.modules_connected || !state.module_reset_supported} onClick={() => openModal(<ConfirmModal strTitle="Reset Magic Modules" strDescription="The controller will briefly disconnect while both modules are reinitialised." strOKButtonText="Reset" onOK={() => void run("modules", () => resetModules(), "Magic Module reset failed", "Both modules reset and detected.")} />)}>{pendingAction === "modules" ? <Spinner /> : "Reset"}</StackedAction>
      </PanelSection>
    </>}

    {activeSection === "audio" && <>
      <PanelSection title="Smart Amp Tuning">
        <PanelSectionRow><ToggleField label="AYANEO Speaker Tuning" description={state.audio_fix_supported ? "Load the official AYANEO 3 profile for both CS35L41 smart amplifiers." : "The required SteamOS firmware or CS35L41 controls are unavailable."} checked={state.audio_fix_enabled} disabled={busy || !state.audio_fix_supported} onChange={enabled => void run("audio", () => setAudioFix(enabled), "Audio fix failed", enabled ? "AYANEO speaker tuning enabled." : "Generic audio profile restored.")} /></PanelSectionRow>
        <PanelSectionRow><Field label={state.audio_fix_installed ? state.audio_profile : state.audio_profile === "Pending" ? "Applying..." : "Not applied"} description={state.audio_fix_error || (state.audio_fix_installed ? "Both speaker DSPs use the device-specific v0.65 tuning." : "SteamOS is using the generic Cirrus speaker profile.")} /></PanelSectionRow>
        <StackedAction title="Reapply Audio Fix" description="Reload the tuning if audio was reset by the system." disabled={busy || !state.audio_fix_enabled || !state.audio_fix_supported} onClick={() => void run("audio", () => reapplyAudioFix(), "Audio fix failed", "AYANEO speaker tuning reapplied.")}>{pendingAction === "audio" ? <Spinner /> : "Apply"}</StackedAction>
      </PanelSection>
      {state.audio_calibration_available && <PanelSection title="Speaker Calibration">
        <PanelSectionRow><Field label={state.audio_calibration_last || "Factory calibration active"} description="Measures both speakers, validates the result and saves it to EFI. The previous calibration is backed up first." /></PanelSectionRow>
        <StackedAction title="Recalibrate Audio" description="Requires a quiet room and a clear surface." disabled={busy} onClick={() => openModal(<ConfirmModal strTitle="Recalibrate Speakers" strDescription="Place the console on a clear surface in a quiet room. A calibration signal will play and the result will be written to EFI." strOKButtonText="Recalibrate" bDestructiveWarning onOK={() => void run("audio", () => recalibrateAudio(), "Audio recalibration failed", "Calibration saved. Restart the console to apply it.")} />)}>{pendingAction === "audio" ? <Spinner /> : "Start"}</StackedAction>
      </PanelSection>}
    </>}

    {activeSection === "buttons" && <PanelSection title="Controller Buttons">
      <PanelSectionRow><ToggleField label="Fix Key Binding" description="Program LC1/RC1 as L5/R5 while preserving native LC/RC as L4/R4. Menu/QAM variants are detected automatically." checked={state.button_fix_installed} disabled={busy} onChange={enabled => void run("buttons", () => setButtonFix(enabled), "Key binding fix failed", enabled ? "Key binding fix enabled." : "Native key mapping restored.")} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="TM Guard" description="Return the controller to custom gamepad mode after an accidental TM press." checked={state.tm_guard_enabled} disabled={busy} onChange={enabled => void run("buttons", () => setTmGuard(enabled), "TM Guard failed", enabled ? "TM Guard enabled." : "TM Guard disabled.")} /></PanelSectionRow>
      <PanelSectionRow><Field label={state.tm_guard_status} description={state.tm_guard_recoveries ? `Recovered ${state.tm_guard_recoveries} TM mode change${state.tm_guard_recoveries === 1 ? "" : "s"} since plugin start.` : "The physical TM button remains available when TM Guard is disabled."} /></PanelSectionRow>
    </PanelSection>}

    {activeSection === "screen" && <PanelSection title="Display Definition">
      <PanelSectionRow><ToggleField label="Install Display Definition" description={state.screen_conflict ? "An existing display definition must be replaced before OLED Fix can be enabled." : "Install the AYANEO 3 OLED HDR, colour and 60/90/120/144 Hz gamescope definition with Gamma 2.2 output."} checked={state.screen_installed} disabled={busy} onChange={changeScreenFix} /></PanelSectionRow>
      <PanelSectionRow><Field label={state.edid_patched ? "EDID metadata normalized" : "EDID update waiting"} description={state.edid_game_nits ? `Games see AYANEO's advertised ${state.edid_game_nits}-nit maximum.` : "Waiting for gamescope to publish the display EDID."} /></PanelSectionRow>
      <PanelSectionRow><Field label="Restart required after changing" description="Restart Game Mode manually so gamescope reloads the display definition." /></PanelSectionRow>
    </PanelSection>}

    {activeSection === "about" && <>
      <PanelSection title="AYANEO 3 Companion">
        <PanelSectionRow><Field label={`Version ${state.version}`} description="Native AYANEO 3 hardware controls for Decky Loader." /></PanelSectionRow>
        <PanelSectionRow><Field label="Rayek" description="BSD-3-Clause open-source plugin." /></PanelSectionRow>
      </PanelSection>
      <UpdateSection initialVersion={state.version} />
    </>}

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
      ++updateGeneration;
      updateListeners.clear();
      updateView = { info: null, checking: false, downloading: false, path: null };
    },
  };
});
