// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// Backend calls, toasts, shared helpers and live readings of the TDP page (tdp.tsx).

import { addEventListener, callable, removeEventListener, toaster, useQuickAccessVisible } from "@decky/api";
import { useEffect, useState } from "react";
import { errorText, settle, type Result } from "./result";
import {
  fromAbsolute, resolvedPreset, simpleTuning,
  type Caps, type CapsInfo, type CpuPowerControls, type PresetTable, type RunningLimits, type TdpInfo,
} from "./tdp_model";

// ── Helpers ────────────────────────────────────────────────────────────────────
export const toMw  = (w: number)  => w * 1000;
export const toW   = (mw: number) => Math.round(mw / 1000);

// The slider model, presets and texts are in tdp_model.ts.

/** Slider view of saved limits (mW). In simple mode a Custom profile follows the single-dial rule. */
export function savedTuning(limits: { spl: number; sppt: number; fppt: number }, stored: string | undefined,
                     table: PresetTable, advanced: boolean, caps: Caps, minW: number) {
  const spl = toW(limits.spl), sppt = toW(limits.sppt), fppt = toW(limits.fppt);
  const preset = resolvedPreset(spl, sppt, fppt, stored, table);
  return { preset, tuning: !advanced && preset === "custom" ? simpleTuning(spl, caps, minW) : fromAbsolute(spl, sppt, fppt) };
}

// ── Types ──────────────────────────────────────────────────────────────────────
export interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean;
                       active_preset?: string; advanced_tdp_control?: boolean; running?: RunningLimits }
export interface TdpResult extends Result<never> { skipped?: boolean; returncode?: number }
export interface PowerSource { ac: boolean }
export interface GameProfile {
  error?: string;
  exists: boolean;
  // False: the profile is kept but unused; the game runs on Global settings.
  active?: boolean;
  profile: { spl: number; sppt: number; fppt: number; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; ac_preset?: string };
}
export interface RunningGame { appId: string; name: string }
export interface ReadyState  { ready: boolean; error: string }

// ── Backend callables ──────────────────────────────────────────────────────────
export const isReady           = callable<[], Result<ReadyState>>("is_ready");
export const getSettings       = callable<[], Result<Settings>>("get_settings");
export const getCaps           = callable<[], Result<CapsInfo>>("get_caps");
export const applyTdp          = callable<[number, number, number, string, string, string | null], TdpResult>("apply_tdp");
export const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
export const getGameProfile    = callable<[string], Result<GameProfile>>("get_game_profile");
export const deleteGameProfile = callable<[string], TdpResult>("delete_game_profile");
export const setGameProfileActive = callable<[string, boolean], TdpResult>("set_game_profile_active");
export const setPluginEnabled  = callable<[boolean], TdpResult>("set_plugin_enabled");
export const setPanelActive    = callable<[boolean], Result<never>>("set_panel_active");
export const setActiveApp      = callable<[string], Result<never>>("set_active_app");
export const getPowerSource    = callable<[], Result<PowerSource>>("get_power_source");
export const reapply           = callable<[], TdpResult>("reapply");
export const setGameAcProfile  = callable<[string, number, number, number, boolean, string], TdpResult>("set_game_ac_profile");
export const retryHelper = callable<[], Result<never>>("retry_helper");
export const setAdvancedTdpControlCall = callable<[boolean], TdpResult>("set_advanced_tdp_control");
export const getCpuPowerControls = callable<[string, boolean], CpuPowerControls>("get_cpu_power_controls");
export const setCpuBoost       = callable<[boolean, string, boolean, string], CpuPowerControls>("set_cpu_boost");
export const setEpp            = callable<[string, string, boolean, string], CpuPowerControls>("set_epp");

// ── Toasts ─────────────────────────────────────────────────────────────────────

export const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error("[ayaneo3companion] tdp.notification_failed");
  }
};

export const notifyFailure = (title: string, err: unknown) => {
  const body = errorText(err);
  console.error("[ayaneo3companion] tdp.operation_failed");
  notify(title, body);
};

// ── Live TDP readings ──────────────────────────────────────────────────────────

// Must stay comfortably under _PANEL_ACTIVE_TTL_S in tdp_backend.py (90 s).
const PANEL_LEASE_MS = 30000;

/** Live limits and package draw while the page is shown; null until the first reading. */
export const useTdpInfo = ({ visible: tabShown = true }: { visible?: boolean } = {}) => {
  const [info, setInfo] = useState<TdpInfo | null>(null);
  const visible = useQuickAccessVisible() && tabShown;

  // Gated on visibility, not just on mount: the page stays mounted while the
  // Quick Access Menu is on another tab, and refreshing it there costs a RAPL
  // read every two seconds for nobody to look at. set_panel_active is what
  // gates the backend loop, so nothing is computed while this is unmounted.
  //
  // The backend pushes each refresh rather than answering a poll: it already
  // recomputed these numbers on exactly this cadence.
  //
  // The lease is renewed rather than set once. The cleanup below drops it, but
  // it never runs if the frontend is torn down outright - a Steam UI restart -
  // and the backend would then keep refreshing forever. Thirty seconds against
  // the backend's ninety leaves room for two lost calls.
  useEffect(() => {
    if (!visible) return;
    let active = true;
    // This advisory lease can expire when Decky disconnects or withdraws the
    // module. A failed renewal/cleanup must not escape as an unhandled promise.
    const updateLease = (value: boolean) => { void settle(setPanelActive(value)); };
    updateLease(true);
    const lease = setInterval(() => updateLease(true), PANEL_LEASE_MS);
    const onInfo = (next: TdpInfo) => { if (active) setInfo(next); };
    addEventListener<[TdpInfo]>("tdp_info", onInfo);
    // Seed it: the first push is a full interval away.
    // An answer without values is an unexpected backend error: ignored, like a rejected call.
    void settle(getTdpInfo()).then((v) => { if (active && v.values) setInfo(v); });
    return () => {
      active = false;
      clearInterval(lease);
      removeEventListener<[TdpInfo]>("tdp_info", onInfo);
      updateLease(false);
    };
  }, [visible]);
  return info;
};
