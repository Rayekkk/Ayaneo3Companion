// SPDX-License-Identifier: BSD-3-Clause
// TDP and CPU rules, texts and the Custom slider model of the TDP page, adapted from
// LegionGo2Companion's desktop/native/tdp.mjs for the AYANEO 3 (RyzenAdj only, no Extras).

import type { Result } from "./result";

// ── Types ─────────────────────────────────────────────────────────────────────
export interface Tuning { spl: number; spptOff: number; fpptOff: number }
export interface Caps { spl: number; sppt: number; fppt: number }
export type PresetKey = "silent" | "balanced" | "performance" | "custom";
export type PresetTable = Record<Exclude<PresetKey, "custom">, Caps>;
export interface CapsInfo {
  min: number; max: Caps; presets?: PresetTable; tdp_available?: boolean; tdp_error?: string;
  owner?: string; readback?: boolean; helper_retry_available?: boolean;
}
export interface CapsState { tdpAvailable: boolean; helperRetryAvailable: boolean; tdpError: string; readback: boolean }
export interface TdpValues { spl_limit?: number; sppt_limit?: number; fppt_limit?: number; package_draw?: number; source?: string }
export interface TdpInfo extends Result<never> { values?: TdpValues; sample_age_seconds?: number | null }
export interface RunningLimits { spl: number; sppt: number; fppt: number; preset: PresetKey; game: boolean }
export interface SettingsLimits {
  enabled?: boolean; running?: RunningLimits; spl?: number; sppt?: number; fppt?: number;
  active_spl?: number; active_sppt?: number; active_fppt?: number; active_preset?: string;
}
export interface CpuBoostControl { available: boolean; enabled: boolean | null; error: string }
export interface EppControl {
  compatibility_note?: string; available: boolean; value: string | null; profiles: string[];
  numeric_supported: boolean; numeric_value: number | null; min: number; max: number; error: string;
}
/** Shared payload for the CPU power controls getter and both setters. */
export interface CpuPowerControls extends Result<never> {
  available: boolean; cpu_boost: CpuBoostControl; epp: EppControl; error: string;
  profile?: { app_id: string; ac_profile: boolean; active: boolean; cpu_boost_enabled: boolean | null; epp: string | null };
}

// ── Custom slider model ───────────────────────────────────────────────────────
// SPL is the dial; slow and fast boost are headroom above it, so raising the TDP carries them along and no
// slider position breaks FPPT >= SPPT >= SPL or the ceilings. A tuning is {spl, spptOff, fpptOff} in watts.
// The ceilings the SMU keeps (SPL 30, SPPT 43 W) and the chosen FPPT ceiling; get_caps() returns the same.
export const FALLBACK_MAX: Caps = { spl: 30, sppt: 43, fppt: 45 };
export const FALLBACK_MIN = 5;

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

/** Headroom still available above the current SPL, per parameter. */
export const offsetMax = (spl: number, caps: Caps) => ({
  sppt: Math.max(0, caps.sppt - spl),
  fppt: Math.max(0, caps.fppt - spl),
});

/** Force a tuning back inside the ceilings, keeping SPPT <= FPPT. */
export function normalise(t: Tuning, caps: Caps, minW: number): Tuning {
  const spl = clamp(t.spl, minW, caps.spl), max = offsetMax(spl, caps);
  const spptOff = clamp(t.spptOff, 0, max.sppt);
  return { spl, spptOff, fpptOff: Math.max(clamp(t.fpptOff, 0, max.fppt), spptOff) };
}

export const absolute = (t: Tuning): Caps => ({ spl: t.spl, sppt: t.spl + t.spptOff, fppt: t.spl + t.fpptOff });
export const fromAbsolute = (spl: number, sppt: number, fppt: number): Tuning =>
  ({ spl, spptOff: Math.max(0, sppt - spl), fpptOff: Math.max(0, fppt - spl) });

/** Single-dial Custom mode: SPL within the range, boosts 10 and 15 W above it (the backend's rule). */
export function simpleTuning(spl: number, caps: Caps, minW: number): Tuning {
  const bounded = clamp(spl, minW, caps.spl);
  return fromAbsolute(bounded, Math.min(bounded + 10, caps.sppt), Math.min(bounded + 15, caps.fppt));
}

/** Moving SPL re-clamps both offsets: near the ceiling there is less headroom left. */
export const withSpl = (t: Tuning, v: number, caps: Caps, minW: number) =>
  normalise({ spl: v, spptOff: t.spptOff, fpptOff: t.fpptOff }, caps, minW);

/** Pushing SPPT up drags FPPT along so SPPT never overtakes it. */
export function withSppt(t: Tuning, v: number, caps: Caps): Tuning {
  const spptOff = clamp(v, 0, offsetMax(t.spl, caps).sppt);
  return { spl: t.spl, spptOff, fpptOff: Math.max(t.fpptOff, spptOff) };
}

/** Pulling FPPT below SPPT drags SPPT down to meet it. */
export function withFppt(t: Tuning, v: number, caps: Caps): Tuning {
  const fpptOff = clamp(v, 0, offsetMax(t.spl, caps).fppt);
  return { spl: t.spl, spptOff: Math.min(t.spptOff, fpptOff), fpptOff };
}

// ── Presets ───────────────────────────────────────────────────────────────────
// Used until get_caps() answers. The backend remains the source of truth.
export const PRESETS: PresetTable = {
  silent: { spl: 10, sppt: 15, fppt: 20 },
  balanced: { spl: 15, sppt: 25, fppt: 30 },
  performance: { spl: 30, sppt: 43, fppt: 45 },
};
export const PRESET_LABELS: Record<PresetKey, string> = {
  silent: "Silent", balanced: "Balanced", performance: "Performance", custom: "Custom",
};
export const PRESET_ORDER: PresetKey[] = ["silent", "balanced", "performance", "custom"];

function detectPreset(spl: number, sppt: number, fppt: number, table: PresetTable = PRESETS): PresetKey {
  for (const key of Object.keys(table) as Array<Exclude<PresetKey, "custom">>) {
    const v = table[key];
    if (v.spl === spl && v.sppt === sppt && v.fppt === fppt) return key;
  }
  return "custom";
}

/** The saved preset name while its values still match, otherwise the preset these values are, or Custom. */
export function resolvedPreset(spl: number, sppt: number, fppt: number, stored: string | undefined,
                               table: PresetTable): PresetKey {
  if (stored === "custom") return "custom";
  const value = stored ? table[stored as Exclude<PresetKey, "custom">] : undefined;
  return value && value.spl === spl && value.sppt === sppt && value.fppt === fppt
    ? stored as PresetKey : detectPreset(spl, sppt, fppt, table);
}

/** The backend's `running` from get_settings ({spl, sppt, fppt} in W, preset, game) when it is complete. */
export function runningNow(settings: SettingsLimits | null | undefined): RunningLimits | null {
  const run = settings?.running;
  return run && [run.spl, run.sppt, run.fppt].every(Number.isFinite) && PRESET_LABELS[run.preset] ? run : null;
}

/** What runs now according to get_settings, in watts with its preset label; null while unknown. */
export function activeLimits(settings: SettingsLimits | null | undefined, table: PresetTable = PRESETS):
    { spl: number; sppt: number; fppt: number; global: boolean; label: string } | null {
  const run = runningNow(settings);
  if (run) return { spl: run.spl, sppt: run.sppt, fppt: run.fppt, global: run.game !== true, label: PRESET_LABELS[run.preset] };
  const spl = settings?.active_spl ?? settings?.spl, sppt = settings?.active_sppt ?? settings?.sppt;
  const fppt = settings?.active_fppt ?? settings?.fppt;
  if (spl == null || sppt == null || fppt == null) return null;
  const toW = (value: number) => Math.round(value / 1000);
  // The saved name is Global's; a game's limits are named by their values.
  const global = spl === settings?.spl && sppt === settings?.sppt && fppt === settings?.fppt;
  const w = toW(spl), sw = toW(sppt), fw = toW(fppt);
  return { spl: w, sppt: sw, fppt: fw, global,
    label: PRESET_LABELS[resolvedPreset(w, sw, fw, global ? settings?.active_preset : undefined, table)] };
}

/** The TDP summary on the overview's TDP row. */
export function overviewSummary(settings: SettingsLimits | null | undefined, table: PresetTable = PRESETS): string {
  if (settings?.enabled === false) return "Off · firmware limits";
  const now = activeLimits(settings, table);
  return now ? `${now.label} · ${now.spl} / ${now.sppt} / ${now.fppt} W` : "Custom";
}

/** The note under the selected preset. */
export const presetNote = (key: PresetKey, table: PresetTable) => key === "custom" || !table[key as Exclude<PresetKey, "custom">]
  ? "Set your own limits below."
  : `${PRESET_LABELS[key]} · ${table[key as Exclude<PresetKey, "custom">].spl} W (boost ${table[key as Exclude<PresetKey, "custom">].sppt}/${table[key as Exclude<PresetKey, "custom">].fppt} W)`;

// ── Power texts ───────────────────────────────────────────────────────────────
/** The note under "Enable TDP control": who sets the limits, the limits now and the draw. `now` is in watts. */
export function powerSummary(enabled: boolean, owner: string, label: string, now: Caps, draw: number | null | undefined): string {
  if (!enabled) return "Off · Companion leaves the limits to the firmware.";
  return `${owner}: ${label} · ${now.spl} W${draw == null ? "" : ` · drawing ${Number(draw).toFixed(1)} W`}`;
}

/** The note under "Settings for <game>"; use is "global", "game" or "charger". */
export const scopeNote = (use: "global" | "game" | "charger", name: string) => use === "global"
  ? `${name} uses Global settings, shared by all games.`
  : use === "game" ? `Only ${name} uses these settings.` : `${name} has separate settings on battery and on charger.`;

export const sourceNote = (acOnline: boolean, charger: boolean) => `${acOnline ? "Plugged in now." : "On battery now."}${
  charger === acOnline ? "" : charger ? " Changes apply when you plug in." : " Changes apply when you unplug."}`;

export const boostRoomNote = (spl: number, offset: number, room: number) =>
  room === 0 ? "No room left at this power." : `Up to ${spl + offset} W.`;

/** What get_caps says about the RyzenAdj helper and the other TDP writer. */
export function capsState(caps: Partial<CapsInfo> | null | undefined): CapsState {
  const c = caps || {};
  const available = c.tdp_available ?? true;
  return { tdpAvailable: available, helperRetryAvailable: c.helper_retry_available === true,
    readback: c.readback === true,
    tdpError: c.tdp_error || (available ? "" : "The verified RyzenAdj helper is unavailable.") };
}

/** Why the limits are not what the settings say, as [title, detail], or null. */
export function tdpNotice(state: CapsState): [string, string] | null {
  return state.tdpAvailable ? null : ["TDP control unavailable", state.tdpError];
}

const wattText = (value: number | undefined) => value == null ? "–" : Number(value).toFixed(0);
/** The "Limits now" readout from get_tdp_info; the kernel blocks it on stock SteamOS. */
export const limitsText = (info: TdpInfo | null | undefined) => info?.ok && info.values?.spl_limit != null
  ? `SPL ${wattText(info.values.spl_limit)} · SPPT ${wattText(info.values.sppt_limit)} · FPPT ${wattText(info.values.fppt_limit)} W` : "—";
/** The package draw readout from get_tdp_info. */
export const drawText = (info: TdpInfo | null | undefined) =>
  info?.ok && info.values?.package_draw != null ? `${Number(info.values.package_draw).toFixed(1)} W` : "—";

// ── CPU ───────────────────────────────────────────────────────────────────────
export const EPP_NAMED_VALUES: Record<string, number> = { performance: 0, balance_performance: 128, balance_power: 191, power: 255 };
export const EPP_PREFERENCE = "Left: faster CPU. Right: more power left for the GPU.";

export function eppLabel(profile: string | null | undefined): string {
  if (!profile) return "Unknown";
  const labels: Record<string, string> = { default: "System default", performance: "Prefer CPU",
    balance_performance: "Balanced · Prefer CPU", balance_power: "Balanced · Prefer GPU", power: "Prefer GPU" };
  if (labels[profile]) return labels[profile];
  const readable = String(profile).replace(/[-_]+/g, " ").trim();
  return readable.charAt(0).toUpperCase() + readable.slice(1);
}

function parseNumericEpp(value: string | null | undefined): number | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 255 ? parsed : null;
}

/** An inactive profile shows its saved CPU target; hardware readback stays authoritative for the active one. */
export function editorControls(controls: CpuPowerControls): CpuPowerControls {
  const profile = controls?.profile;
  if (!profile || profile.active) return controls;
  return Object.assign({}, controls, {
    cpu_boost: Object.assign({}, controls.cpu_boost, { enabled: profile.cpu_boost_enabled }),
    epp: Object.assign({}, controls.epp, { value: profile.epp, numeric_value: parseNumericEpp(profile.epp) }),
  });
}

/** The slider position in percent, rounded to the UI's step; 50% while the system sets the value. */
export function eppPercent(epp: EppControl, step: number): number {
  const named = epp?.value ? EPP_NAMED_VALUES[epp.value] : undefined;
  const raw = epp?.numeric_value ?? parseNumericEpp(epp?.value) ?? named ?? 128;
  return clamp(Math.round(raw * 100 / 255 / step) * step, 0, 100);
}
export const eppValue = (percent: number) => String(Math.round(clamp(percent, 0, 100) * 255 / 100));

export const eppTitle = (epp: EppControl) => epp?.numeric_supported && parseNumericEpp(epp.value) != null
  ? "CPU energy preference" : `CPU energy preference · ${eppLabel(epp?.value)}`;
export const eppUnavailable = (epp: EppControl) => epp?.error || "Not supported on this device.";

/** The note under the numeric energy slider (the page shows its own errors and "Applying…" first). */
export function eppNote(epp: EppControl): string {
  if (!epp?.available) return eppUnavailable(epp);
  const systemSet = parseNumericEpp(epp.value) == null && (epp.value == null || EPP_NAMED_VALUES[epp.value] == null);
  return systemSet ? "Set by the system. Move the slider to choose." : EPP_PREFERENCE;
}

/** CPU Boost can change only while it is supported and its state could be read. */
export const boostUsable = (boost: CpuBoostControl) => !!boost?.available && boost.enabled != null;
export const boostNote = (boost: CpuBoostControl) => !boost?.available ? boost?.error || "Not supported on this device."
  : boost.enabled == null ? boost.error || "The current state could not be read."
  : boost.enabled ? "Higher CPU clocks; uses more power." : "CPU stays at base clock; saves power.";
