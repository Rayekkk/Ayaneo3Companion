// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { PanelSection, PanelSectionRow, SliderField, Spinner, ToggleField } from "@decky/ui";
import { FC } from "react";
import type { BatteryStatus, State, Vibration } from "./model";
import { infoRow } from "./shared";
import { StackedAction } from "./ui";

const titleCase = (value: string) => value ? value[0].toUpperCase() + value.slice(1) : value;
const formatDuration = (seconds: number): string => {
  const minutes = Math.max(1, Math.round(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  if (!hours) return `${minutes} min`;
  return remainder ? `${hours} hr ${remainder} min` : `${hours} hr`;
};
const vibrationLevels: Vibration[] = ["off", "low", "medium", "high"];
const controllerUnavailable = (state: State) =>
  state.controller_error ? infoRow("Controller control unavailable", state.controller_error) : null;

export const VibrationPage: FC<{
  state: State;
  busy: boolean;
  testing: boolean;
  onLevel(value: number): void;
  onGain(value: number): void;
  onTest(): void;
}> = ({ state, busy, testing, onLevel, onGain, onTest }) => <PanelSection title="Vibration">
  {controllerUnavailable(state)}
  <PanelSectionRow><SliderField label={`Firmware Strength · ${titleCase(state.controller.vibration)}`} disabled={busy || state.controller_supported === false} description="Off · Low · Medium · High" value={vibrationLevels.indexOf(state.controller.vibration)} min={0} max={3} step={1} notchCount={4} notchTicksVisible validValues="steps" minimumDpadGranularity={1} showValue={false} onChange={onLevel} /></PanelSectionRow>
  <PanelSectionRow><SliderField label="FF Gain" description="Scales force-feedback effects from games and vibration tests." value={state.controller.ff_gain} min={0} max={100} step={10} notchCount={11} notchTicksVisible validValues="steps" minimumDpadGranularity={10} showValue valueSuffix="%" onChange={onGain} disabled={busy} /></PanelSectionRow>
  <StackedAction title="Test Vibration" description="Play a short 500 ms rumble pulse." disabled={busy || state.controller.vibration === "off" || state.controller.ff_gain === 0} onClick={onTest}>{testing ? <Spinner /> : "Test"}</StackedAction>
  {infoRow("Two-stage control", "Firmware Strength selects the controller's base level; FF Gain scales Linux force-feedback effects from 0 to 100%.")}
</PanelSection>;

export const BatteryPage: FC<{
  state: State;
  battery: BatteryStatus | null;
  error: string;
  busy: boolean;
  onBypass(enabled: boolean): void;
}> = ({ state, battery, error, busy, onBypass }) => <>
  <PanelSection title="Battery Status">
    {!battery
      ? (error ? infoRow("Battery status unavailable", `${error} Retrying automatically.`) : <PanelSectionRow><Spinner /></PanelSectionRow>)
      : infoRow(
          battery.available ? `${battery.percent == null ? "Battery" : `${battery.percent}%`} · ${battery.status}` : "Battery unavailable",
          battery.seconds_to_full != null
            ? `Estimated time to full: ${formatDuration(battery.seconds_to_full)}${battery.power_w == null ? "" : ` · ${battery.power_w.toFixed(1)} W net charge`}`
            : battery.status.toLowerCase() === "charging" ? "Calculating the charging estimate..."
            : battery.status.toLowerCase() === "full" ? "The battery is fully charged."
            : "Time to full is shown while the battery is charging.")}
    {battery && error && infoRow("Battery refresh failed", `${error} Showing the last confirmed battery values. Retrying automatically.`)}
  </PanelSection>
  <PanelSection title="Charge Control">
    <PanelSectionRow><ToggleField label="Bypass Charging" description={state.charge_bypass_supported ? "Power the console from the charger without charging the battery." : "AYANEO charge control is not exposed by the current system."} checked={state.charge_bypass} disabled={busy || !state.charge_bypass_supported} onChange={onBypass} /></PanelSectionRow>
    {infoRow(state.charge_bypass ? "Bypass active" : "Automatic charging", "State verified through the AYANEO kernel or embedded-controller interface.")}
  </PanelSection>
</>;

const moduleId = (code: number | null) => code == null ? "" : `Module ID 0x${code.toString(16).toUpperCase().padStart(2, "0")}`;

export const MagicModulesPage: FC<{
  state: State;
  busy: boolean;
  pending: boolean;
  onEject(side: "left" | "right" | "both"): void;
  onReset(): void;
}> = ({ state, busy, pending, onEject, onReset }) => <>
  {controllerUnavailable(state)}
  <PanelSection title="Installed Modules">
    {infoRow(`Left · ${state.module_left.label}`, [state.module_left.layout, moduleId(state.module_left.code)].filter(Boolean).join(" · "))}
    {infoRow(`Right · ${state.module_right.label}`, [state.module_right.layout, moduleId(state.module_right.code)].filter(Boolean).join(" · "))}
  </PanelSection>
  <PanelSection title="Controller Eject">
    {infoRow(state.modules_reconnecting ? "Waiting for both modules" : state.modules_connected ? "Both modules connected" : "Module disconnected",
      "After ejection, insert both modules to initialise the controller again.")}
    {(["left", "right", "both"] as const).map(side => <StackedAction key={side} title={`Eject ${titleCase(side)}`} description={side === "both" ? "Release both controller modules." : `Release the ${side} controller module.`} disabled={busy || state.modules_reconnecting || !state.module_eject_supported} onClick={() => onEject(side)}>{pending ? <Spinner /> : "Eject"}</StackedAction>)}
  </PanelSection>
  <PanelSection title="Recovery">
    <StackedAction title="Reset Magic Modules" description="Reinitialise both modules and restore lighting, vibration, FF Gain and rear-button mappings." disabled={busy || !state.modules_connected || !state.module_reset_supported} onClick={onReset}>{pending ? <Spinner /> : "Reset"}</StackedAction>
  </PanelSection>
</>;

export const AudioPage: FC<{
  state: State;
  busy: boolean;
  pending: boolean;
  onEnabled(enabled: boolean): void;
  onReapply(): void;
  onRecalibrate(): void;
}> = ({ state, busy, pending, onEnabled, onReapply, onRecalibrate }) => <>
  <PanelSection title="Smart Amp Tuning">
    <PanelSectionRow><ToggleField label="AYANEO Speaker Tuning" description={state.audio_fix_supported ? "Load the official AYANEO 3 profile for both CS35L41 smart amplifiers." : state.audio_profile === "Checking" || state.audio_profile === "Pending" ? "Waiting for the SteamOS speaker controls." : "The required SteamOS firmware or CS35L41 controls are unavailable."} checked={state.audio_fix_enabled} disabled={busy || !state.audio_fix_supported} onChange={onEnabled} /></PanelSectionRow>
    {infoRow(state.audio_fix_installed ? state.audio_profile : state.audio_profile === "Pending" ? "Applying..." : "Not applied",
      state.audio_fix_error || (state.audio_profile === "AYANEO v0.65" ? "Both speaker DSPs use the device-specific v0.65 tuning."
        : state.audio_profile === "Pending" || state.audio_profile === "Checking" ? "Waiting to verify the speaker profile."
        : state.audio_fix_installed ? "Tuning is installed, but the active speaker profile has not been confirmed."
        : "SteamOS is using the generic Cirrus speaker profile."))}
    <StackedAction title="Reapply Audio Fix" description="Reload the tuning if audio was reset by the system." disabled={busy || !state.audio_fix_enabled || !state.audio_fix_supported} onClick={onReapply}>{pending ? <Spinner /> : "Apply"}</StackedAction>
  </PanelSection>
  {state.audio_calibration_available && <PanelSection title="Speaker Calibration">
    {infoRow(state.audio_calibration_last || "Factory calibration active", "Measures both speakers, validates the result and saves it to EFI. The previous calibration is backed up first.")}
    <StackedAction title="Recalibrate Audio" description="Requires a quiet room and a clear surface." disabled={busy} onClick={onRecalibrate}>{pending ? <Spinner /> : "Start"}</StackedAction>
  </PanelSection>}
</>;

export const ButtonsPage: FC<{
  state: State;
  busy: boolean;
  onButtonFix(enabled: boolean): void;
  onTmGuard(enabled: boolean): void;
}> = ({ state, busy, onButtonFix, onTmGuard }) => <PanelSection title="Controller Buttons">
  {controllerUnavailable(state)}
  <PanelSectionRow><ToggleField label="Fix Key Binding" description="Program LC1/RC1 as L5/R5 while preserving native LC/RC as L4/R4. Menu/QAM variants are detected automatically." checked={state.button_fix_installed} disabled={busy || state.controller_supported === false} onChange={onButtonFix} /></PanelSectionRow>
  <PanelSectionRow><ToggleField label="TM Guard" description="Return the controller to custom gamepad mode after an accidental TM press." checked={state.tm_guard_enabled} disabled={busy} onChange={onTmGuard} /></PanelSectionRow>
  {infoRow(state.tm_guard_status, state.tm_guard_recoveries ? `Recovered ${state.tm_guard_recoveries} TM mode change${state.tm_guard_recoveries === 1 ? "" : "s"} since plugin start.` : "The physical TM button remains available when TM Guard is disabled.")}
</PanelSection>;

export const DisplayPage: FC<{
  state: State;
  busy: boolean;
  onChange(enabled: boolean): void;
}> = ({ state, busy, onChange }) => <PanelSection title="Display Definition">
  <PanelSectionRow><ToggleField label="Install Display Definition" description={state.screen_conflict ? "An existing display definition must be replaced before OLED Fix can be enabled." : "Install the AYANEO 3 OLED HDR, colour and 60/90/120/144 Hz gamescope definition with Gamma 2.2 output."} checked={state.screen_installed} disabled={busy} onChange={onChange} /></PanelSectionRow>
  {infoRow(state.edid_patched ? "EDID metadata normalized" : "EDID update waiting", state.edid_game_nits ? `Games see AYANEO's advertised ${state.edid_game_nits}-nit maximum.` : "Waiting for gamescope to publish the display EDID.")}
  {infoRow("Restart required after changing", "Restart Game Mode manually so gamescope reloads the display definition.")}
</PanelSection>;
