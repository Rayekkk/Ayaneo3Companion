// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
import { DropdownItem, Field, gamepadSliderClasses, PanelSection, PanelSectionRow, SliderField, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { PageShell, SectionHeader, SectionLink, StackedAction, type SectionKey } from "./ui";
import { confirmAction, infoRow } from "./shared";
import { resetUpdates } from "./updates";
import { AboutPage } from "./about";
import { AudioPage, BatteryPage, ButtonsPage, DisplayPage, MagicModulesPage, VibrationPage } from "./device_pages";
import { FanPage, resetFanStatus, useFanControl } from "./fan";
import { TdpPage, startTdpWatcher, stopTdpWatcher } from "./tdp";
import { overviewSummary, type SettingsLimits } from "./tdp_model";
import type { BatteryStatus, Controller, Hsv, RgbMode, State, Vibration } from "./model";

const SECTION_TITLES: Record<SectionKey, string> = {
  tdp: "TDP", fan: "Fan Control", vibration: "Vibration", rgb: "Lighting", buttons: "Key Binding",
  modules: "Magic Modules", battery: "Battery", audio: "Audio", screen: "OLED Display", about: "About",
};
const readAge = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`;
};
type ActionKey = "vibration" | "battery" | "modules" | "audio" | "buttons" | "screen";
// A write acknowledges its own settings; other fields in its fast response
// may predate the latest hardware observation. Full polls replace diagnostics.
const ACTION_FIELDS: Record<ActionKey, ReadonlyArray<keyof State>> = {
  vibration: ["controller"],
  battery: ["charge_bypass", "charge_bypass_supported"],
  modules: ["modules_reconnecting", "modules_connected", "module_left", "module_right"],
  audio: ["audio_fix_enabled", "audio_fix_installed", "audio_calibration_available",
    "audio_calibration_last", "audio_profile", "audio_fix_error"],
  buttons: ["button_fix_installed", "tm_guard_enabled", "tm_guard_status", "tm_guard_recoveries"],
  screen: ["screen_installed", "screen_conflict", "screen_restart_needed", "screen_native"],
};

const getState = callable<[], State>("get_state");
const getBatteryStatus = callable<[], BatteryStatus>("get_battery_status");
// TDP Control has its own page and RPCs (tdp.tsx); the overview row reads its settings.
const getTdpSettings = callable<[], SettingsLimits>("get_settings");
const setController = callable<[Partial<Controller>], State>("set_controller");
const setControllerWithVibrationFeedback = callable<[Partial<Controller>], State>("set_controller_with_vibration_feedback");
const setVibrationGain = callable<[number], State>("set_vibration_gain");
const testVibration = callable<[number], { success: boolean; error?: string }>("test_vibration");
const setChargeBypass = callable<[boolean], State>("set_charge_bypass");
const ejectModules = callable<["left" | "right" | "both"], State>("eject_modules");
const resetModules = callable<[], State>("reset_modules");
const setScreenFix = callable<[boolean, boolean?], State>("set_screen_fix");
const setButtonFix = callable<[boolean], State>("set_button_fix");
const restartGameMode = callable<[], State>("restart_game_mode");
const setTmGuard = callable<[boolean], State>("set_tm_guard");
const setAudioFix = callable<[boolean], State>("set_audio_fix");
const reapplyAudioFix = callable<[], State>("reapply_audio_fix");
const recalibrateAudio = callable<[], State>("recalibrate_audio");
const VIBRATION_LEVELS: Vibration[] = ["off", "low", "medium", "high"];
const VIBRATION_TEST_MS = 500;
const VIBRATION_APPLY_DELAY_MS = 150;
const rgbOptions = ["off", "solid", "pulse", "rainbow"].map(data => ({ data, label: data[0].toUpperCase() + data.slice(1) }));
const titleCase = (value: string) => value ? value[0].toUpperCase() + value.slice(1) : value;
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

const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
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
  // QAM open and the Companion tab actually shown: Steam can hide the tab with display:none.
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  const panelOnScreen = () => !panelRef.current || panelRef.current.getClientRects().length > 0;
  const lastOpenedSection = useRef<SectionKey | null>(null);
  const lastReadAt = useRef<number | null>(null);
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
  const [tdpSettings, setTdpSettings] = useState<SettingsLimits | null>(null);
  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null);
  const pendingActionRef = useRef<ActionKey | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [stateReadError, setStateReadError] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [battery, setBattery] = useState<BatteryStatus | null>(null);
  const [batteryReadError, setBatteryReadError] = useState("");
  const readingBattery = useRef(false);
  const refreshBattery = useRef<(() => Promise<void>) | null>(null);
  const busy = pendingAction !== null || Boolean(state?.settings_error || state?.initializing);
  const controllerBusy = busy || state?.controller_supported === false;
  const notifyFailure = (title: string, error: unknown) => {
    const body = error instanceof Error ? error.message : String(error);
    try { toaster.toast({ title, body }); } catch { console.error(`[ayaneo3companion] ${title}: ${body}`); }
  };
  const acceptState = (response: State, fields?: ReadonlyArray<keyof State>) => {
    if (!mounted.current) return;
    const next = fields && confirmedState
      ? { ...confirmedState, ...Object.fromEntries(fields.map(key => [key, response[key]])) }
      : response;
    confirmedState = next;
    setState(current => current ? { ...next,
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

  const refresh = useCallback(async () => {
    if (!mounted.current || !visibleRef.current || reading.current || pendingActionRef.current || controllerWriting.current || controllerPending.current) return;
    reading.current = true;
    const request = revision.current;
    try {
      const next = await getState();
      if (!mounted.current || !visibleRef.current || request !== revision.current) return;
      acceptState(next);
      lastReadAt.current = Date.now();
      setStateReadError(null);
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
    const element = panelRef.current;
    if (!element) return;
    setOnScreen(panelOnScreen());
    const Observer = (element.ownerDocument.defaultView as (Window & typeof globalThis) | null)?.IntersectionObserver;
    if (!Observer) return;
    // Steam can hide the Decky tab with display:none while QAM remains open.
    const observer = new Observer(entries => setOnScreen(entries.some(entry => entry.isIntersecting)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    // Overlays such as a native dropdown or a confirmation hide Quick Access briefly.
    // Visibility controls polling only; the open page belongs to the links and back button.
    ++revision.current;
    if (!visible) { flushPendingController.current(); return; }
    if (!onScreen) return;
    void refresh();
    const timer = setInterval(refresh, 1500);
    return () => clearInterval(timer);
  }, [refresh, visible, onScreen]);

  // The TDP row on the overview: what runs now, read from TDP Control's own store.
  useEffect(() => {
    if (!visible || !onScreen || activeSection) return;
    let cancelled = false, reading = false;
    const read = () => {
      if (reading) return;
      reading = true;
      getTdpSettings().then(next => { if (!cancelled) setTdpSettings(next); })
        .catch(() => undefined).finally(() => { reading = false; });
    };
    read();
    const timer = setInterval(read, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [visible, onScreen, activeSection]);

  useEffect(() => {
    if (!visible || activeSection !== "battery") return;
    let cancelled = false;
    const updateBattery = async () => {
      if (cancelled || readingBattery.current) return;
      readingBattery.current = true;
      try {
        const next = await getBatteryStatus();
        if (!cancelled) { setBattery(next); setBatteryReadError(""); }
      } catch (error) {
        if (!cancelled) setBatteryReadError(error instanceof Error ? error.message : String(error));
      } finally {
        readingBattery.current = false;
        // Navigation invalidates the reply, not the running backend worker.
        // Start the reopened page's read only after that worker has completed.
        if (cancelled) void refreshBattery.current?.();
      }
    };
    refreshBattery.current = updateBattery;
    void updateBattery();
    const timer = setInterval(() => void updateBattery(), 10000);
    return () => {
      cancelled = true;
      if (refreshBattery.current === updateBattery) refreshBattery.current = null;
      clearInterval(timer);
    };
  }, [activeSection, visible]);

  const finishAction = () => {
    ++revision.current;
    pendingActionRef.current = null;
    if (mounted.current) {
      setPendingAction(null);
      void refresh();
    }
  };

  const run = async (action: ActionKey, work: () => Promise<State>, title: string, success?: string) => {
    if (pendingActionRef.current || state?.settings_error || state?.initializing) return;
    const canAccept = () => mounted.current;
    ++revision.current;
    pendingActionRef.current = action;
    setPendingAction(action); setStatus(null);
    try {
      const next = await work();
      if (canAccept()) { acceptState(next, ACTION_FIELDS[action]); if (success) setStatus(success); }
    } catch (error) {
      notifyFailure(title, error);
      if (canAccept()) setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      finishAction();
    }
  };

  if (!state) return <PageShell rootRef={panelRef}><PanelSection><PanelSectionRow>{status ? <Field label="Backend unavailable" description={status.replace(/^Error:\s*/, "")} /> : <Spinner />}</PanelSectionRow></PanelSection></PageShell>;
  if (state.settings_error || state.startup_error || state.initializing) return <PageShell rootRef={panelRef}><PanelSection title="Plugin unavailable">{infoRow(state.initializing ? "Starting hardware controls" : "Settings require attention", state.settings_error || state.startup_error || "Waiting for the backend to finish initialising.")}</PanelSection></PageShell>;
  if (!state.supported) return <PageShell rootRef={panelRef}><PanelSection title="Unsupported device">{infoRow(state.device, "AYANEO 3 is required.")}</PanelSection></PageShell>;

  const changeScreenFix = (enabled: boolean) => {
    const apply = (replaceExisting = false) => void run(
      "screen",
      () => setScreenFix(enabled, replaceExisting),
      "Display fix failed",
      enabled ? "Display definition installed. Restart Game Mode." : "Display definition removed. Restart Game Mode.",
    );
    if (enabled && state.screen_conflict) {
      confirmAction("Replace Display Definition?",
        "Another AYANEO 3 gamescope display definition is already installed. OLED Fix must replace it to manage the display. The existing file will be backed up before any change.",
        "Replace", () => apply(true));
      return;
    }
    apply();
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

  const openSection = (section: SectionKey) => {
    lastOpenedSection.current = section;
    setStatus(null); setActiveSection(section);
  };
  const backToControls = () => { setStatus(null); setActiveSection(null); };
  const focusSection = lastOpenedSection.current;
  // One status read serves every row: an older confirmed state says how old it is.
  const staleNote = stateReadError && lastReadAt.current !== null
    ? `Older data · Last read ${readAge(Date.now() - lastReadAt.current)} ago · Retrying…` : undefined;
  const link = (section: SectionKey, description: string, note = staleNote) =>
    <SectionLink section={section} title={SECTION_TITLES[section]} description={description} statusNote={note}
      restoreFocus={focusSection === section} onClick={() => openSection(section)} />;
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

  if (!activeSection) return <PageShell rootRef={panelRef}>
    <PanelSection title="Hardware Controls">
      {link("tdp", tdpSettings ? overviewSummary(tdpSettings) : "Power limits and CPU settings", undefined)}
      {fan.available && link("fan",
        fan.error || !fan.status?.supported ? "Status unavailable" : `${fan.status.mode === "manual" && fan.status.percent != null ? `Manual · ${fan.status.percent}%` : "Auto"}${fan.status.rpm == null ? "" : ` · ${fan.status.rpm} RPM`}`,
        fan.error || undefined)}
      {link("vibration", `${titleCase(state.controller.vibration)} · FF Gain ${state.controller.ff_gain}%`)}
      {link("rgb", state.controller.rgb_mode === "off" ? "Off" : `${titleCase(state.controller.rgb_mode)} · #${state.controller.color.toUpperCase()}`)}
      {link("buttons", `${state.button_fix_installed ? "L5/R5 enabled" : "Native mapping"} · TM Guard ${state.tm_guard_enabled ? "on" : "off"}`)}
      {link("modules", moduleSummary)}
      {link("battery", state.charge_bypass ? "Bypass charging active" : "Automatic charging")}
      {link("audio", audioSummary)}
      {link("screen", state.screen_restart_needed ? "Restart Gaming Mode to finish"
        : state.screen_native ? `SteamOS display profile${state.edid_game_nits ? ` · ${state.edid_game_nits} nits` : ""}`
        : state.screen_installed ? `Definition installed${state.edid_game_nits ? ` · ${state.edid_game_nits} nits` : ""}`
        : state.screen_conflict ? "Existing definition requires confirmation" : "Display definition not installed")}
    </PanelSection>
    <PanelSection title="Device">
      {infoRow(state.device, "SteamOS")}
    </PanelSection>
    <PanelSection title="Plugin">
      {link("about", `AYANEO 3 Companion · v${state.version}`, undefined)}
    </PanelSection>
  </PageShell>;

  return <PageShell rootRef={panelRef}>
    <SectionHeader title={SECTION_TITLES[activeSection]} onBack={backToControls} />
    {readFailure}

    {activeSection === "tdp" && <TdpPage visible={onScreen} />}

    {activeSection === "vibration" && <VibrationPage state={state} busy={busy} testing={pendingAction === "vibration"} onLevel={setVibrationLevel} onGain={setFfGain} onTest={() => void runVibrationTest()} />}

    {activeSection === "fan" && <FanPage status={fan.status} available={fan.available} error={fan.error} busy={fan.busy} onApply={(mode, percent) => void fan.apply(mode, percent)} />}

    {activeSection === "rgb" && <PanelSection title="LED Settings">
      {state.controller_error && <PanelSectionRow><Field label="Controller control unavailable" description={state.controller_error} /></PanelSectionRow>}
      <PanelSectionRow><ToggleField label="Enable LED Control" disabled={controllerBusy} checked={state.controller.rgb_mode !== "off"} onChange={enabled => applyController({ rgb_mode: enabled ? lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED Mode" disabled={controllerBusy} selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onChange={option => applyController({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
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
      state={state} battery={battery} error={batteryReadError} busy={busy}
      onBypass={enabled => void run("battery", () => setChargeBypass(enabled), "Charge bypass failed", enabled ? "Charging bypass enabled." : "Automatic charging restored.")}
    />}

    {activeSection === "modules" && <MagicModulesPage
      state={state} busy={busy} pending={pendingAction === "modules"}
      onEject={side => confirmAction(`Eject ${titleCase(side)}`,
        `Release ${side === "both" ? "both controller modules" : `the ${side} controller module`}?`, "Eject",
        () => void run("modules", () => ejectModules(side), "Module eject failed", `${titleCase(side)} module${side === "both" ? "s" : ""} released.`))}
      onReset={() => confirmAction("Reset Magic Modules",
        "The controller will briefly disconnect while both modules are reinitialised.", "Reset",
        () => void run("modules", () => resetModules(), "Magic Module reset failed", "Both modules reset and detected."))}
    />}

    {activeSection === "audio" && <AudioPage
      state={state} busy={busy} pending={pendingAction === "audio"}
      onEnabled={enabled => void run("audio", () => setAudioFix(enabled), "Audio fix failed", enabled ? "AYANEO speaker tuning enabled." : "Generic audio profile restored.")}
      onReapply={() => void run("audio", () => reapplyAudioFix(), "Audio fix failed", "AYANEO speaker tuning reapplied.")}
      onRecalibrate={() => confirmAction("Recalibrate Speakers",
        "Place the console on a clear surface in a quiet room. A calibration signal will play and the result will be written to EFI.",
        "Recalibrate",
        () => void run("audio", () => recalibrateAudio(), "Audio recalibration failed", "Calibration saved. Restart the console to apply it."))}
    />}

    {activeSection === "buttons" && <ButtonsPage
      state={state} busy={busy}
      onButtonFix={enabled => void run("buttons", () => setButtonFix(enabled), "Key binding fix failed", enabled ? "Key binding fix enabled." : "Native key mapping restored.")}
      onTmGuard={enabled => void run("buttons", () => setTmGuard(enabled), "TM Guard failed", enabled ? "TM Guard enabled." : "TM Guard disabled.")}
    />}

    {activeSection === "screen" && <DisplayPage state={state} busy={busy} onChange={changeScreenFix}
      onRestart={() => confirmAction("Restart Gaming Mode?", "Steam and any running game will close and reopen. Save your game first.",
        "Restart", () => void run("screen", () => restartGameMode(), "Gaming Mode restart failed", "Gaming Mode is restarting."))} />}

    {activeSection === "about" && <AboutPage version={state.version} />}

    {status && activeSection !== "tdp" && <PanelSection title="Status"><PanelSectionRow><Field label={status.startsWith("Error:") ? "Action failed" : "Done"} description={status.replace(/^Error:\s*/, "")} /></PanelSectionRow></PanelSection>}
  </PageShell>;
};

const Icon: FC = () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: "1em", height: "1em" }}><path d="M7 8h10a5 5 0 0 1 4.7 6.7l-1.1 3.1a2 2 0 0 1-3.3.8L15 16H9l-2.3 2.6a2 2 0 0 1-3.3-.8l-1.1-3.1A5 5 0 0 1 7 8Z"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="12" r="2"/></svg>;
export default definePlugin(() => {
  // Per-game profiles must keep following Steam even while the QAM is closed.
  startTdpWatcher();
  return {
    name: "AYANEO 3 Companion",
    titleView: <div className={staticClasses.Title}>AYANEO 3 Companion</div>,
    content: <Content />,
    alwaysRender: true,
    icon: <Icon />,
    onDismount() {
      stopTdpWatcher(); confirmedState = null;
      resetFanStatus();
      resetUpdates();
    },
  };
});
