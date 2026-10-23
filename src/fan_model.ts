// SPDX-License-Identifier: BSD-3-Clause
// Fan Control texts and curve editing for the Fan page.
// LegionGo2Companion's desktop/native/fan.mjs for the AYANEO 3: speeds are the EC's duty in percent.

export type Curve = [number, number][];
export interface FanLimits { min_pct: number; last_min_pct: number; max_pct: number; step: number; hard_max: number;
  rpm_at_min: number; rpm_per_pct: number }

export const STARTING_STUCK_MS = 30000;

export const curveName = (name: string) => String(name).charAt(0).toUpperCase() + String(name).slice(1);

/** The save button: the global curve, or the curve of the chosen TDP preset. */
export const saveLabel = (scope: string) => scope === "global" ? "Save global curve" : `Assign curve to ${curveName(scope)}`;

/** The selected point under the graph; the last point (100°C) only changes speed. */
export const pointText = (index: number, count: number, temperature: number, speed: string) =>
  `Point ${index + 1}/${count} · ${temperature}°C · ${speed}${index === count - 1 ? " · speed only" : ""}`;

export const speedText = (pct: number | undefined) => pct === undefined || pct === null || !Number.isFinite(Number(pct)) ? "—%" : `${Math.round(Number(pct))}%`;

/** The duty a healthy fan runs at for a measured speed, to place the live marker on the curve's axis. */
export const pctForRpm = (rpm: number, limits: FanLimits) =>
  rpm <= 0 ? 0 : Math.min(limits.max_pct, Math.max(0, limits.min_pct + (rpm - limits.rpm_at_min) / limits.rpm_per_pct));

type StatusInput = { supported?: boolean; error?: string; active_curve?: string;
  settings?: { enabled?: boolean; assign_tdp_presets?: boolean };
  telemetry?: { active?: boolean; automatic?: boolean }; ownership?: { blocked?: boolean; reason?: string };
  readings?: { valid?: boolean; automatic?: boolean } } | null | undefined;

/**
 * One status for every fan view, evaluated top-down from fields the backend
 * already returns. `startingFor` (ms) is how long the curve has been starting.
 */
export function fanStatus(state: StatusInput, startingFor = 0): { headline: string; detail: string; starting?: boolean } {
  if (!state || typeof state !== "object" || !Object.keys(state).length) return { headline: "Reading fan…", detail: "" };
  const settings = state.settings || {}, telemetry = state.telemetry, ownership = state.ownership || {};
  if (!state.supported) return { headline: "Not available on this console", detail: state.error || "" };
  const enabled = settings.enabled;
  // Pause/retry keeps Fan Control on and reports why; a latched fault turns it off.
  if (enabled && state.error) return { headline: "Paused, resuming automatically", detail: "No action needed." };
  if (enabled && !telemetry?.active) return { headline: "Starting your curve…", starting: true, detail: startingFor >= STARTING_STUCK_MS
    ? "Still starting. Turn Fan Control off and on to retry." : "No action needed." };
  if (!enabled && state.error) return { headline: "Turned off after an error", detail: `${state.error} Turn on to try again.` };
  if (!enabled && ownership.blocked) return { headline: "Another app controls the fan", detail: ownership.reason || "" };
  if (enabled) return { headline: !state.active_curve || state.active_curve === "global" ? "Global curve" : `${curveName(state.active_curve)} TDP curve`,
    detail: "Turn off to return the fan to automatic." };
  if (state.readings?.valid && state.readings.automatic === false) return { headline: "Another app controls the fan", detail: "" };
  if (telemetry && !telemetry.automatic) return { headline: "Returning fan to automatic…", detail: "No action needed." };
  return { headline: "Automatic", detail: settings.assign_tdp_presets
    ? "Global curve with optional Companion TDP assignments." : "Uses your saved global curve." };
}

/** Set one axis of one point. A speed between 0 and the minimum running speed snaps to the nearer of the two. */
export function editPoint(curve: Curve, index: number, axis: 0 | 1, value: number, limits: FanLimits): Curve {
  const next: Curve = curve.map(([t, p]) => [t, p]);
  const last = index === curve.length - 1;
  if (!Number.isFinite(value) || !Number.isInteger(index) || index < 0 || index >= curve.length || last && axis === 0) return next;
  if (axis === 0) {
    next[index][0] = Math.max(index ? curve[index - 1][0] + 1 : 0, Math.min(curve[index + 1][0] - 1, Math.round(value)));
  } else {
    let pct = Math.round(value / limits.step) * limits.step;
    if (pct > 0 && pct < limits.min_pct) pct = pct >= limits.min_pct / 2 ? limits.min_pct : 0;
    // The 100°C point stays at 100°C and never runs below its own floor.
    next[index][1] = Math.max(index ? curve[index - 1][1] : 0, last ? limits.last_min_pct : 0,
      Math.min(last ? limits.max_pct : curve[index + 1][1], limits.max_pct, pct));
  }
  return next;
}

/** A speed step from a button or key: up from a stopped fan starts at the minimum, down below it stops. */
export function nudgeSpeed(pct: number, delta: number, minPct: number) {
  const next = pct + delta;
  return delta > 0 && pct < minPct ? Math.max(next, minPct) : delta < 0 && next < minPct ? 0 : next;
}
