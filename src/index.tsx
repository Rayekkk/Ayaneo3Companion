// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, DropdownItem, Field, gamepadSliderClasses, PanelSection, PanelSectionRow, Router, SliderField, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, ReactNode, useCallback, useEffect, useRef, useState } from "react";

type Vibration = "off" | "low" | "medium" | "high";
type RgbMode = "off" | "solid" | "pulse" | "rainbow";
type Preset = "Low power" | "Balanced" | "Performance" | "Max" | "Custom";
interface Tdp { spl: number; sppt: number; fppt: number }
interface Tuning { spl: number; spptOff: number; fpptOff: number }
interface Controller { vibration: Vibration; rgb_mode: RgbMode; color: string; brightness: number }
interface Hsv { hue: number; saturation: number; brightness: number }
interface RunningGame { appId: string; name: string }
interface GameProfile { exists: boolean; profile: Tdp }
interface State { supported: boolean; device: string; tdp_backend: string; tdp: Tdp; presets: Record<string, Tdp>; controller: Controller; gpu_power_w: number | null; screen_installed: boolean; edid_patched: boolean; edid_game_nits: number; button_fix_installed: boolean }

const getState = callable<[], State>("get_state");
const setTdp = callable<[Tdp], State>("set_tdp");
const getGameProfile = callable<[string], GameProfile>("get_game_profile");
const setGameProfile = callable<[string, Tdp], State>("set_game_profile");
const deleteGameProfile = callable<[string], State>("delete_game_profile");
const setActiveApp = callable<[string], void>("set_active_app");
const setController = callable<[Controller], State>("set_controller");
const testVibration = callable<[number], { success: boolean; error?: string }>("test_vibration");
const setScreenFix = callable<[boolean], State>("set_screen_fix");
const setButtonFix = callable<[boolean], State>("set_button_fix");
const CLOSED = { tdp: false, vibration: false, rgb: false, buttons: false, screen: false };
const PRESET_ORDER: Preset[] = ["Low power", "Balanced", "Performance", "Max", "Custom"];
const vibrationOptions = ["off", "low", "medium", "high"].map(data => ({ data, label: data[0].toUpperCase() + data.slice(1) }));
const rgbOptions = ["off", "solid", "pulse", "rainbow"].map(data => ({ data, label: data[0].toUpperCase() + data.slice(1) }));

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

// HueSync's slider model: update the UI immediately, but commit only once the
// value has remained unchanged for 500 ms.
const SlowSliderField: FC<SlowSliderProps> = slider => {
  const [changeValue, setChangeValue] = useState(slider.value);
  useEffect(() => {
    const timer = setTimeout(() => {
      if (changeValue === slider.value) slider.onChangeEnd(slider.value);
    }, 500);
    return () => clearTimeout(timer);
  }, [changeValue, slider.value]);
  return <SliderField
    label={slider.label} value={slider.value} min={slider.min} max={slider.max}
    validValues="range" showValue valueSuffix={slider.valueSuffix} className={slider.className}
    onChange={value => { slider.onChange(value); setChangeValue(value); }}
  />;
};

function detectPreset(tdp: Tdp, presets: Record<string, Tdp>): Preset {
  for (const name of ["Low power", "Balanced", "Performance", "Max"] as Preset[]) {
    const value = presets[name];
    if (value && value.spl === tdp.spl && value.sppt === tdp.sppt && value.fppt === tdp.fppt) return name;
  }
  return "Custom";
}

const finite = (value: number, fallback: number) => Number.isFinite(value) ? value : fallback;
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
const spptOffsetMax = (spl: number) => Math.max(0, 40 - spl);
const fpptOffsetMax = (spl: number) => Math.max(0, 45 - spl);
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

const Fold: FC<{ title: string; open: boolean; setOpen: (value: boolean) => void; children: ReactNode }> = ({ title, open, setOpen, children }) => <>
  <PanelSection><PanelSectionRow><ButtonItem layout="below" onClick={() => setOpen(!open)}>{open ? "▼" : "▶"} {title}</ButtonItem></PanelSectionRow></PanelSection>
  {open && children}
</>;

const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const wasVisible = useRef(false);
  const presetInitialized = useRef(false);
  const tdpDirty = useRef(false);
  const controllerDirty = useRef(false);
  const controllerPending = useRef<Controller | null>(null);
  const controllerWriting = useRef(false);
  const controllerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastRgbMode = useRef<RgbMode>("solid");
  const [state, setState] = useState<State | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>(CLOSED);
  const [preset, setPreset] = useState<Preset>("Custom");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [game, setGame] = useState<RunningGame | null>(AppWatcher.currentGame());
  const [perGame, setPerGame] = useState(false);
  const gameRequest = useRef(0);

  useEffect(() => {
    if (!state || controllerDirty.current) return;
    const hsv = hexToHsv(state.controller.color);
    setRgbEdit({ ...hsv, brightness: state.controller.brightness });
  }, [state?.controller.color, state?.controller.brightness]);

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
          controller: controllerDirty.current ? current.controller : next.controller,
        };
      });
      if (!presetInitialized.current) { presetInitialized.current = true; setPreset(detectPreset(next.tdp, next.presets)); }
    } catch { /* backend may still be starting */ }
  }, []);

  useEffect(() => {
    if (visible && !wasVisible.current) setOpen(CLOSED);
    wasVisible.current = visible;
    refresh();
    if (!visible) return;
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh, visible]);

  const run = async (work: Promise<State>, title: string, success?: string) => {
    setBusy(true); setStatus(null);
    try { setState(await work); if (success) setStatus(success); }
    catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus(`Error: ${message}`); toaster.toast({ title, body: message }); await refresh();
    } finally { setBusy(false); }
  };
  const fold = (name: string) => ({ open: !!open[name], setOpen: (value: boolean) => setOpen(current => ({ ...current, [name]: value })) });

  if (!state) return <PanelSection><PanelSectionRow><Spinner /></PanelSectionRow></PanelSection>;
  if (!state.supported) return <PanelSection title="Unsupported device"><PanelSectionRow><Field label={state.device} description="AYANEO 3 is required." /></PanelSectionRow></PanelSection>;

  const tdp = state.tdp;
  const tuning = normalise(fromAbsolute(tdp));
  const spptOff = tuning.spptOff;
  const fpptOff = tuning.fpptOff;
  const maxSpptOffset = spptOffsetMax(tuning.spl);
  const maxFpptOffset = fpptOffsetMax(tuning.spl);
  const setCustomTdp = (next: Tdp) => { tdpDirty.current = true; setPreset("Custom"); setState({ ...state, tdp: next }); setStatus(null); };
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
    const value = state.presets[name]; setState({ ...state, tdp: value });
    run(perGame && game ? setGameProfile(game.appId, value) : setTdp(value), "TDP preset failed",
      perGame && game ? `${name} saved for ${game.name}.` : `${name} applied.`);
  };
  const togglePerGame = async (enabled: boolean) => {
    if (!game) return;
    setPerGame(enabled); setStatus(null);
    if (enabled) {
      const profile = await getGameProfile(game.appId);
      if (profile.exists) {
        setPreset(detectPreset(profile.profile, state.presets));
        setState({ ...state, tdp: profile.profile });
      } else {
        setStatus(`Choose a preset or Custom settings for ${game.name}.`);
      }
    } else {
      setBusy(true);
      try {
        const next = await deleteGameProfile(game.appId);
        setState(next); setPreset(detectPreset(next.tdp, next.presets));
        setStatus(`Global TDP restored for ${game.name}.`);
      } catch (error) {
        setPerGame(true);
        const message = error instanceof Error ? error.message : String(error);
        toaster.toast({ title: "Game profile removal failed", body: message });
      } finally { setBusy(false); }
    }
  };
  const flushController = async () => {
    if (controllerWriting.current) return;
    controllerWriting.current = true;
    try {
      while (controllerPending.current) {
        const next = controllerPending.current;
        controllerPending.current = null;
        try {
          const applied = await setController(next);
          if (!controllerPending.current) {
            controllerDirty.current = false;
            setState(current => current ? { ...applied, tdp: current.tdp, controller: next } : applied);
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          toaster.toast({ title: "Controller setting failed", body: message });
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
    setState({ ...state, controller });
    if (controllerTimer.current) clearTimeout(controllerTimer.current);
    controllerTimer.current = setTimeout(() => { controllerTimer.current = null; void flushController(); }, delay);
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
    setBusy(true);
    try {
      const result = await testVibration(500);
      if (!result.success) toaster.toast({ title: "Vibration test failed", body: result.error ?? "Unknown error" });
    } catch (error) {
      toaster.toast({ title: "Vibration test failed", body: error instanceof Error ? error.message : String(error) });
    } finally { setBusy(false); }
  };

  return <>
    <Fold title="TDP" {...fold("tdp")}>
      <PanelSection title="Live Power"><PanelSectionRow><Field label={state.gpu_power_w == null ? "Unavailable" : `${state.gpu_power_w.toFixed(1)} W`} description={`Backend: ${state.tdp_backend}`} /></PanelSectionRow></PanelSection>
      <PanelSection title="Game Profile"><PanelSectionRow><ToggleField label="Per Game Profile" description={game ? game.name : "No game running"} checked={perGame} disabled={!game || busy} onChange={enabled => void togglePerGame(enabled)} /></PanelSectionRow></PanelSection>
      <PanelSection title="Preset">
        {PRESET_ORDER.map(name => <PanelSectionRow key={name}><ButtonItem layout="below" disabled={preset === name || busy} onClick={() => choosePreset(name)}>{preset === name ? `> ${name}` : name}</ButtonItem></PanelSectionRow>)}
        {status && preset !== "Custom" && <PanelSectionRow><div style={{ fontSize: "11px" }}>{status}</div></PanelSectionRow>}
      </PanelSection>
      {preset === "Custom" && <>
        <PanelSection title="TDP Limits">
          <PanelSectionRow><SliderField label={`SPL (TDP) - ${tuning.spl} W`} value={tuning.spl} min={5} max={35} step={1} onChange={setSpl} description="Sustained power limit - the main TDP dial" /></PanelSectionRow>
          <PanelSectionRow><SliderField key={`sppt-${tuning.spl}-${maxSpptOffset}`} label={`SPPT +${spptOff} W  =  ${tuning.spl + spptOff} W`} value={spptOff} min={0} max={maxSpptOffset || 1} step={1} disabled={maxSpptOffset === 0} onChange={setSpptOff} description={maxSpptOffset === 0 ? "No headroom left at this SPL" : `Slow limit headroom above SPL (max 40 W)`} /></PanelSectionRow>
          {state.tdp_backend !== "PowerStation" ? <PanelSectionRow><SliderField key={`fppt-${tuning.spl}-${maxFpptOffset}`} label={`FPPT +${fpptOff} W  =  ${tuning.spl + fpptOff} W`} value={fpptOff} min={0} max={maxFpptOffset || 1} step={1} disabled={maxFpptOffset === 0} onChange={setFpptOff} description={maxFpptOffset === 0 ? "No headroom left at this SPL" : `Fast limit headroom above SPL (max 45 W)`} /></PanelSectionRow> : <PanelSectionRow><Field label="FPPT is automatic" description="PowerStation derives the fast limit from SPL and SPPT." /></PanelSectionRow>}
        </PanelSection>
        <PanelSection title="Action">
          <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => { tdpDirty.current = false; run(perGame && game ? setGameProfile(game.appId, tdp) : setTdp(tdp), "TDP apply failed", perGame && game ? `Custom settings saved for ${game.name}.` : "Custom settings applied."); }}>{busy ? "Applying..." : perGame && game ? `Apply & Save for ${game.name}` : "Apply TDP"}</ButtonItem></PanelSectionRow>
          {status && <PanelSectionRow><div style={{ fontSize: "11px" }}>{status}</div></PanelSectionRow>}
        </PanelSection>
      </>}
    </Fold>

    <Fold title="Vibration" {...fold("vibration")}><PanelSection title="Vibration"><PanelSectionRow><DropdownItem label="Strength" selectedOption={state.controller.vibration} rgOptions={vibrationOptions} onChange={option => applyController({ vibration: option.data as Vibration })} /></PanelSectionRow><PanelSectionRow><ButtonItem layout="below" disabled={busy || state.controller.vibration === "off"} onClick={() => void runVibrationTest()}>{busy ? "Testing..." : "Test Vibration"}</ButtonItem></PanelSectionRow><PanelSectionRow><Field label="Controller firmware setting" description="Applies to both detachable controller modules." /></PanelSectionRow></PanelSection></Fold>
    <Fold title="RGB" {...fold("rgb")}><PanelSection title="Settings">
      <PanelSectionRow><ToggleField label="Enable LED Control" checked={state.controller.rgb_mode !== "off"} onChange={enabled => applyController({ rgb_mode: enabled ? lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED Mode" selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onChange={option => applyController({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
        {(() => {
          const hsv = rgbEdit;
          return <>
            <PanelSectionRow><SlowSliderField label="Hue" value={hsv.hue} min={0} max={359} valueSuffix="°" className="AyaneoRgbHue" onChange={hue => previewRgb({ ...hsv, hue })} onChangeEnd={hue => commitRgb({ ...hsv, hue })} /></PanelSectionRow>
            <PanelSectionRow><SlowSliderField label="Saturation" value={hsv.saturation} min={0} max={100} valueSuffix="%" className="AyaneoRgbSaturation" onChange={saturation => previewRgb({ ...hsv, saturation })} onChangeEnd={saturation => commitRgb({ ...hsv, saturation })} /></PanelSectionRow>
            <PanelSectionRow><SlowSliderField label="Brightness" value={hsv.brightness} min={0} max={100} valueSuffix="%" className="AyaneoRgbBrightness" onChange={brightness => previewRgb({ ...hsv, brightness })} onChangeEnd={brightness => commitRgb({ ...hsv, brightness })} /></PanelSectionRow>
            <style>{`
              .AyaneoRgbHue .${gamepadSliderClasses.SliderTrack} {
                background: linear-gradient(to right, hsl(0,100%,50%), hsl(60,100%,50%), hsl(120,100%,50%), hsl(180,100%,50%), hsl(240,100%,50%), hsl(300,100%,50%), hsl(360,100%,50%)) !important;
                --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important;
              }
              .AyaneoRgbSaturation .${gamepadSliderClasses.SliderTrack} {
                background: linear-gradient(to right, hsl(${hsv.hue},0%,100%), hsl(${hsv.hue},100%,50%)) !important;
                --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important;
              }
              .AyaneoRgbBrightness .${gamepadSliderClasses.SliderTrack} {
                background: linear-gradient(to right, #000, hsl(${hsv.hue},${hsv.saturation}%,50%)) !important;
                --left-track-color: #0000 !important; --colored-toggles-main-color: #0000 !important;
              }
            `}</style>
          </>;
        })()}
      </>}
    </PanelSection></Fold>
    <Fold title="Menu buttons" {...fold("buttons")}><PanelSection title="Menu buttons"><PanelSectionRow><ToggleField label="Fix QAM button" description="Maps AYASpace to Steam and Meta+D to Quick Access using a persistent InputPlumber override." checked={state.button_fix_installed} disabled={busy} onChange={enabled => run(setButtonFix(enabled), "Button fix failed")} /></PanelSectionRow></PanelSection></Fold>
    <Fold title="OLED display" {...fold("screen")}>
      <PanelSection title="OLED display"><PanelSectionRow><ToggleField label="Install display definition" description="Installs the AYANEO 3 OLED HDR, colour and 60/90/120/144 Hz gamescope definition with Gamma 2.2 output. Restart Game Mode after changing it." checked={state.screen_installed} disabled={busy} onChange={enabled => run(setScreenFix(enabled), "Display fix failed")} /></PanelSectionRow><PanelSectionRow><Field label={state.edid_patched ? "Applied" : "Waiting"} description={state.edid_game_nits ? `Games see ${state.edid_game_nits} nits through the corrected EDID.` : "Waiting for gamescope to publish the display EDID."} /></PanelSectionRow></PanelSection>
    </Fold>
  </>;
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
