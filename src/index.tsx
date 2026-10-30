// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/Ayaneo3Companion

import { callable, definePlugin, useQuickAccessVisible } from "@decky/api";
import { DropdownItem, Field, gamepadSliderClasses, PanelSection, PanelSectionRow, Spinner, staticClasses, ToggleField } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction, infoRow, notify, PageHeader, PageShell, SectionLink } from "./ui";
import { unwrap } from "./shared";
import { settle, type Result } from "./result";
import { AboutPage, INSTALL_HINT, UpdateController, UpdateSection, type UpdateCheck, type UpdateDownload } from "./about";
import { SECTION_TITLES, sectionIcons, type SectionKey } from "./sections";
import { useControllerQueue } from "./controller_queue";
import { AudioPage, BatteryPage, ButtonsPage, DisplayPage, MagicModulesPage, VibrationPage } from "./device_pages";
import { FanPage, fanSummary, getFanState, type FanState } from "./fan";
import { clamp, hexToHsv, hsvToHex, SlowSliderField } from "./lighting";
import { TdpPage, startTdpWatcher, stopTdpWatcher } from "./tdp";
import { overviewSummary, type SettingsLimits } from "./tdp_model";
import type { BatteryStatus, Hsv, RgbMode, State, Vibration } from "./model";

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
  screen: ["screen_installed", "screen_conflict", "screen_restart_needed", "screen_native", "screen_fix_enabled"],
};

const getState = callable<[], Result<State>>("get_state");
const getBatteryStatus = callable<[], Result<BatteryStatus>>("get_battery_status");
const getVersion = callable<[], Result<never> & { version?: string; home?: string }>("get_version");
const checkForUpdates = callable<[], UpdateCheck>("check_for_updates");
const performUpdate = callable<[string], UpdateDownload>("perform_update");
// TDP Control has its own page and RPCs (tdp.tsx); the overview row reads its settings.
const getTdpSettings = callable<[], Result<SettingsLimits>>("get_settings");
const testVibration = callable<[number], Result<never>>("test_vibration");
const setChargeBypass = callable<[boolean], Result<State>>("set_charge_bypass");
const ejectModules = callable<["left" | "right" | "both"], Result<State>>("eject_modules");
const resetModules = callable<[], Result<State>>("reset_modules");
const setScreenFix = callable<[boolean, boolean?], Result<State>>("set_screen_fix");
const setButtonFix = callable<[boolean], Result<State>>("set_button_fix");
const restartGameMode = callable<[], Result<State>>("restart_game_mode");
const setTmGuard = callable<[boolean], Result<State>>("set_tm_guard");
const setAudioFix = callable<[boolean], Result<State>>("set_audio_fix");
const reapplyAudioFix = callable<[], Result<State>>("reapply_audio_fix");
const recalibrateAudio = callable<[], Result<State>>("recalibrate_audio");
const VIBRATION_LEVELS: Vibration[] = ["off", "low", "medium", "high"];
const VIBRATION_TEST_MS = 500;
const VIBRATION_APPLY_DELAY_MS = 150;
const rgbOptions = ["off", "solid", "pulse", "rainbow"].map(data => ({ data, label: data[0].toUpperCase() + data.slice(1) }));
const titleCase = (value: string) => value ? value[0].toUpperCase() + value.slice(1) : value;
const INSTALL = `${INSTALL_HINT} Your settings and per-game profiles are kept.`;
let confirmedState: State | null = null;
let updates: UpdateController | null = null;
/** The console user's home folder (get_version), read once About opens: a saved ZIP shows as ~/… */
let home = "";

const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const mounted = useRef(true);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const revision = useRef(0);
  const reading = useRef(false);
  const desiredRgb = useRef<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  // QAM open and the Companion tab actually shown: Steam can hide the tab with display:none.
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  const panelOnScreen = () => !panelRef.current || panelRef.current.getClientRects().length > 0;
  const lastOpenedSection = useRef<SectionKey | null>(null);
  // An open page with unsaved edits (Fan curve, TDP Custom limits) says what leaving it drops; null when nothing would.
  const unsaved = useRef<(() => string | null) | null>(null);
  const guardLeave = (check: (() => string | null) | null) => { unsaved.current = check; };
  const lastReadAt = useRef<number | null>(null);
  const [state, setState] = useState<State | null>(confirmedState);
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [fanState, setFanState] = useState<FanState | undefined>(undefined);
  const [tdpSettings, setTdpSettings] = useState<SettingsLimits | null>(null);
  const [pendingAction, setPendingAction] = useState<ActionKey | null>(null);
  const pendingActionRef = useRef<ActionKey | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [stateReadError, setStateReadError] = useState<string | null>(null);
  const [rgbEdit, setRgbEdit] = useState<Hsv>({ hue: 0, saturation: 100, brightness: 100 });
  const [battery, setBattery] = useState<BatteryStatus | null>(null);
  const [batteryReadError, setBatteryReadError] = useState("");
  const [homeDir, setHomeDir] = useState(home);
  const readingBattery = useRef(false);
  const refreshBattery = useRef<(() => Promise<void>) | null>(null);
  const busy = pendingAction !== null || Boolean(state?.settings_error || state?.initializing);
  const controllerBusy = busy || state?.controller_supported === false;
  const notifyFailure = (title: string, error: unknown) =>
    notify(title, error instanceof Error ? error.message : String(error));
  const acceptState = (response: State, fields?: ReadonlyArray<keyof State>) => {
    if (!mounted.current) return;
    const next = fields && confirmedState
      ? { ...confirmedState, ...Object.fromEntries(fields.map(key => [key, response[key]])) }
      : response;
    confirmedState = next;
    setState(current => current ? { ...next,
      controller: queue.dirty.current ? current.controller : next.controller,
    } : next);
  };
  const queue = useControllerQueue({
    mounted, revision,
    blocked: () => Boolean(pendingActionRef.current || state?.settings_error || state?.initializing),
    shown: () => state!.controller,
    show: controller => setState(current => current ? { ...current, controller } : current),
    applied: applied => {
      confirmedState = confirmedState ? { ...confirmedState, controller: applied.controller } : applied;
      setState(current => current ? { ...current, controller: applied.controller } : applied);
    },
    failed: message => notifyFailure("Controller setting failed", message),
    refresh: () => void refresh(),
  });
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; ++revision.current; queue.flushPending(); };
  }, []);

  useEffect(() => {
    if (!state || queue.dirty.current) return;
    const hsv = hexToHsv(state.controller.color);
    desiredRgb.current = { ...hsv, brightness: state.controller.brightness };
    setRgbEdit(desiredRgb.current);
  }, [state?.controller.color, state?.controller.brightness]);

  useEffect(() => {
    if (state && !queue.dirty.current) {
      queue.desired.current = state.controller;
      if (state.controller.rgb_mode !== "off") queue.lastRgbMode.current = state.controller.rgb_mode;
    }
  }, [state?.controller]);

  const refresh = useCallback(async () => {
    if (!mounted.current || !visibleRef.current || reading.current || pendingActionRef.current || queue.busy()) return;
    reading.current = true;
    const request = revision.current;
    try {
      const next = await unwrap(getState());
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
    if (!visible) { queue.flushPending(); return; }
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
      unwrap(getTdpSettings()).then(next => { if (!cancelled) setTdpSettings(next); })
        .catch(() => undefined).finally(() => { reading = false; });
    };
    read();
    const timer = setInterval(read, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [visible, onScreen, activeSection]);

  // The Fan row: measured numbers expire after 2.5 s, so it reads every second like Fan Control's own page.
  useEffect(() => {
    if (!visible || !onScreen || activeSection) return;
    let cancelled = false, reading = false;
    const read = () => {
      if (reading) return;
      reading = true;
      getFanState(() => !cancelled).then(next => { if (!cancelled) setFanState(next); })
        .catch(() => { if (!cancelled) setFanState(previous => previous && { ...previous, readings: { valid: false, error: "" } }); })
        .finally(() => { reading = false; });
    };
    read();
    const timer = setInterval(read, 1000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [visible, onScreen, activeSection]);

  useEffect(() => {
    if (!visible || activeSection !== "battery") return;
    let cancelled = false;
    const updateBattery = async () => {
      if (cancelled || readingBattery.current) return;
      readingBattery.current = true;
      try {
        const next = await unwrap(getBatteryStatus());
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

  // A saved update shows as ~/…: the home folder is read once, when About first opens.
  useEffect(() => {
    if (activeSection !== "about" || home) return;
    let active = true;
    void settle(getVersion()).then(result => {
      if (result.ok && result.home) { home = result.home; if (active) setHomeDir(home); }
    });
    return () => { active = false; };
  }, [activeSection]);

  const finishAction = () => {
    ++revision.current;
    pendingActionRef.current = null;
    if (mounted.current) {
      setPendingAction(null);
      void refresh();
    }
  };

  /** `announce`: the action ran from a dialog that hid the panel, so its success is a toast too (UI.md §5). */
  const run = async (action: ActionKey, work: () => Promise<Result<State>>, title: string, success?: string, announce?: string) => {
    if (pendingActionRef.current || state?.settings_error || state?.initializing) return;
    const canAccept = () => mounted.current;
    ++revision.current;
    pendingActionRef.current = action;
    setPendingAction(action); setStatus(null);
    try {
      const next = await unwrap(work());
      if (canAccept()) { acceptState(next, ACTION_FIELDS[action]); if (success) setStatus(success); }
      if (announce && success) notify(announce, success);
    } catch (error) {
      notifyFailure(title, error);
      if (canAccept()) setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      finishAction();
    }
  };

  // A plugin that did not start keeps its updater (UI.md §3).
  const updateSection = updates && <UpdateSection controller={updates} version={state?.version || __PLUGIN_VERSION__} home={homeDir} install={INSTALL} />;
  if (!state) return <PageShell rootRef={panelRef}><PanelSection><PanelSectionRow>{status ? <Field focusable label="Backend unavailable" description={status.replace(/^Error:\s*/, "")} /> : <Spinner />}</PanelSectionRow></PanelSection>{status && updateSection}</PageShell>;
  if (state.settings_error || state.startup_error || state.initializing) return <PageShell rootRef={panelRef}><PanelSection title="Plugin unavailable">{infoRow(state.initializing ? "Starting hardware controls" : "Settings require attention", state.settings_error || state.startup_error || "Waiting for the backend to finish initializing.")}</PanelSection>{!state.initializing && updateSection}</PageShell>;
  if (!state.supported) return <PageShell rootRef={panelRef}><PanelSection title="Unsupported device">{infoRow(state.device, "AYANEO 3 is required.")}</PanelSection></PageShell>;

  const changeScreenFix = (enabled: boolean) => {
    const apply = (replaceExisting = false) => void run(
      "screen",
      () => setScreenFix(enabled, replaceExisting),
      "Display fix failed",
      enabled ? "Display definition installed. Restart Gaming Mode." : "Display definition removed. Restart Gaming Mode.",
      replaceExisting ? SECTION_TITLES.screen : undefined,
    );
    if (enabled && state.screen_conflict) {
      confirmAction("Replace display definition?",
        "Another AYANEO 3 gamescope display definition is already installed. OLED Fix must replace it to manage the display. The existing file will be backed up before any change.",
        "Replace", () => apply(true));
      return;
    }
    apply();
  };

  const setVibrationLevel = (value: number) => {
    if (!Number.isFinite(value)) return;
    const vibration = VIBRATION_LEVELS[Math.round(clamp(value, 0, VIBRATION_LEVELS.length - 1))];
    if (vibration !== queue.current().vibration) queue.apply({ vibration }, VIBRATION_APPLY_DELAY_MS);
  };
  const setFfGain = (raw: number) => {
    if (!Number.isFinite(raw)) return;
    const value = Math.round(clamp(raw, 0, 100) / 10) * 10;
    if (value !== queue.current().ff_gain) queue.apply({ ff_gain: value }, 150);
  };
  const previewRgb = (next: Hsv) => {
    queue.dirty.current = true;
    desiredRgb.current = next;
    setRgbEdit(next);
  };
  const commitRgb = (next: Hsv, field: "color" | "brightness") => {
    desiredRgb.current = next;
    setRgbEdit(next);
    queue.apply(field === "color" ? { color: hsvToHex(next.hue, next.saturation) } : { brightness: next.brightness }, 500);
  };
  const runVibrationTest = async () => {
    if (pendingActionRef.current || state.settings_error || state.initializing) return;
    ++revision.current;
    pendingActionRef.current = "vibration";
    setPendingAction("vibration");
    queue.cancelTimer();
    let controllerConfirmed = false;
    try {
      controllerConfirmed = await queue.flush();
      if (!controllerConfirmed || !mounted.current) return;
      const result = await testVibration(VIBRATION_TEST_MS);
      if (!result.ok) notifyFailure("Vibration test failed", result.error ?? "Unknown error");
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
  const leave = () => { setStatus(null); setActiveSection(null); };
  // B and ‹ All Controls: unsaved edits are dropped only after "Discard changes?"; Cancel stays on the page.
  const backToControls = () => {
    const lost = unsaved.current?.();
    if (!lost) { leave(); return; }
    confirmAction("Discard changes?", lost, "Discard", leave, { destructive: true });
  };
  const focusSection = lastOpenedSection.current;
  // One status read serves every row: an older confirmed state says how old it is.
  const staleNote = stateReadError && lastReadAt.current !== null
    ? `Older data · Last read ${readAge(Date.now() - lastReadAt.current)} ago · Retrying…` : undefined;
  // Coming back from a page focuses its row once, and only while the menu is shown (SectionLink reads it on mount).
  const link = (section: SectionKey, description: string, note = staleNote) =>
    <SectionLink icon={sectionIcons[section]} title={SECTION_TITLES[section]} description={description} note={note}
      focus={visible && focusSection === section} onOpen={() => openSection(section)} />;
  const moduleSummary = state.modules_reconnecting
    ? "Waiting for both modules"
    : state.modules_connected
      ? `${state.module_left.label} · ${state.module_right.label}`
      : "A module is disconnected";
  const audioSummary = state.audio_fix_enabled
    ? state.audio_fix_installed ? state.audio_profile : "Tuning pending"
    : "Generic SteamOS profile";
  const readFailure = stateReadError && <PanelSection title="Device status">
    <PanelSectionRow><Field focusable label="Status refresh failed"
      description={`Showing last confirmed values. Retrying automatically. ${stateReadError}`} /></PanelSectionRow>
  </PanelSection>;

  if (!activeSection) return <PageShell rootRef={panelRef}>
    <PanelSection title="Hardware controls">
      {link("tdp", tdpSettings ? overviewSummary(tdpSettings) : "Power limits and CPU settings", undefined)}
      {link("fan", fanSummary(fanState), undefined)}
      {link("vibration", `${titleCase(state.controller.vibration)} · FF Gain ${state.controller.ff_gain}%`)}
      {link("rgb", state.controller.rgb_mode === "off" ? "Off" : `${titleCase(state.controller.rgb_mode)} · #${state.controller.color.toUpperCase()}`)}
      {link("buttons", `${state.button_fix_installed ? "LC1/RC1 enabled" : "Native mapping"} · TM Guard ${state.tm_guard_enabled ? "on" : "off"}`)}
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
      {link("about", `Version ${state.version} · updates`, undefined)}
    </PanelSection>
  </PageShell>;

  return <PageShell rootRef={panelRef} onBack={backToControls}>
    <PageHeader title={SECTION_TITLES[activeSection]} back="All Controls" onBack={backToControls} focus={visible} />
    {readFailure}

    {activeSection === "tdp" && <TdpPage visible={onScreen} guardLeave={guardLeave} />}

    {activeSection === "vibration" && <VibrationPage state={state} busy={busy} testing={pendingAction === "vibration"} onLevel={setVibrationLevel} onGain={setFfGain} onTest={() => void runVibrationTest()} />}

    {activeSection === "fan" && <FanPage viewVisible={onScreen} isViewVisible={panelOnScreen} guardLeave={guardLeave} />}

    {activeSection === "rgb" && <PanelSection title="LED settings">
      {state.controller_error && <PanelSectionRow><Field label="Controller control unavailable" description={state.controller_error} /></PanelSectionRow>}
      <PanelSectionRow><ToggleField label="Enable LED control" disabled={controllerBusy} checked={state.controller.rgb_mode !== "off"} onChange={enabled => queue.apply({ rgb_mode: enabled ? queue.lastRgbMode.current : "off" })} /></PanelSectionRow>
      {state.controller.rgb_mode !== "off" && <>
        <PanelSectionRow><DropdownItem label="LED mode" disabled={controllerBusy} selectedOption={state.controller.rgb_mode} rgOptions={rgbOptions.filter(option => option.data !== "off")} onChange={option => queue.apply({ rgb_mode: option.data as RgbMode })} /></PanelSectionRow>
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
      onEject={side => confirmAction(side === "both" ? "Eject both modules?" : `Eject the ${side} module?`,
        `${side === "both" ? "Both controller modules are" : `The ${side} controller module is`} released. Insert ${side === "both" ? "them" : "it"} again to use the controller.`, "Eject",
        () => void run("modules", () => ejectModules(side), "Module eject failed", `${titleCase(side)} module${side === "both" ? "s" : ""} released.`, SECTION_TITLES.modules))}
      onReset={() => confirmAction("Reset Magic Modules?",
        "The controller will briefly disconnect while both modules are reinitialized.", "Reset",
        () => void run("modules", () => resetModules(), "Magic Module reset failed", "Both modules reset and detected.", SECTION_TITLES.modules))}
    />}

    {activeSection === "audio" && <AudioPage
      state={state} busy={busy} pending={pendingAction === "audio"}
      onEnabled={enabled => void run("audio", () => setAudioFix(enabled), "Audio fix failed", enabled ? "AYANEO speaker tuning enabled." : "Generic audio profile restored.")}
      onReapply={() => void run("audio", () => reapplyAudioFix(), "Audio fix failed", "AYANEO speaker tuning reapplied.")}
      onRecalibrate={() => confirmAction("Recalibrate speakers?",
        "Place the console on a clear surface in a quiet room. A calibration signal will play and the result will be written to EFI.",
        "Recalibrate",
        () => void run("audio", () => recalibrateAudio(), "Audio recalibration failed", "Calibration saved. Restart the console to apply it.", SECTION_TITLES.audio))}
    />}

    {activeSection === "buttons" && <ButtonsPage
      state={state} busy={busy}
      onButtonFix={enabled => void run("buttons", () => setButtonFix(enabled), "Key binding fix failed", enabled ? "Key binding fix enabled." : "Native key mapping restored.")}
      onTmGuard={enabled => void run("buttons", () => setTmGuard(enabled), "TM Guard failed", enabled ? "TM Guard enabled." : "TM Guard disabled.")}
    />}

    {activeSection === "screen" && <DisplayPage state={state} busy={busy} onChange={changeScreenFix}
      onRestart={() => confirmAction("Restart Gaming Mode?", "Steam and any running game will close and reopen. Save your game first.",
        "Restart", () => void run("screen", () => restartGameMode(), "Gaming Mode restart failed", "Gaming Mode is restarting.", SECTION_TITLES.screen),
        { destructive: true })} />}

    {activeSection === "about" && <AboutPage name="AYANEO 3 Companion" version={state.version}
      description="All-in-one AYANEO 3 hardware controls for Decky Loader." license="BSD-3-Clause" source="github.com/Rayekkk/Ayaneo3Companion"
      credits={[["Included modules", "TDP & CPU · Fan Control · Vibration · Lighting · Key binding · Magic Modules · Battery · Audio · OLED display"]]}
      updates={updates ?? undefined} home={homeDir} install={INSTALL} />}

    {status && activeSection !== "tdp" && <PanelSection title="Status"><PanelSectionRow><Field focusable label={status.startsWith("Error:") ? "Action failed" : "Done"} description={status.replace(/^Error:\s*/, "")} /></PanelSectionRow></PanelSection>}
  </PageShell>;
};

const Icon: FC = () => <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" style={{ width: "1em", height: "1em" }}><path d="M7 8h10a5 5 0 0 1 4.7 6.7l-1.1 3.1a2 2 0 0 1-3.3.8L15 16H9l-2.3 2.6a2 2 0 0 1-3.3-.8l-1.1-3.1A5 5 0 0 1 7 8Z"/><circle cx="8" cy="12" r="2"/><circle cx="16" cy="12" r="2"/></svg>;
export default definePlugin(() => {
  // Per-game profiles must keep following Steam even while the QAM is closed.
  startTdpWatcher();
  updates?.dispose();
  updates = new UpdateController({ check: checkForUpdates, download: performUpdate }, "ayaneo3companion");
  return {
    name: "AYANEO 3 Companion",
    titleView: <div className={staticClasses.Title}>AYANEO 3 Companion</div>,
    content: <Content />,
    alwaysRender: true,
    icon: <Icon />,
    onDismount() {
      stopTdpWatcher(); confirmedState = null;
      updates?.dispose(); updates = null; home = "";
    },
  };
});
