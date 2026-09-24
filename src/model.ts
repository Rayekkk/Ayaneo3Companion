// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

export type Vibration = "off" | "low" | "medium" | "high";
export type RgbMode = "off" | "solid" | "pulse" | "rainbow";
export type Preset = "Minimum" | "Low power" | "Balanced" | "Performance" | "Max" | "Custom";
export interface Tdp { spl: number; sppt: number; fppt: number }
export interface Controller { vibration: Vibration; ff_gain: number; rgb_mode: RgbMode; color: string; brightness: number }
export interface Hsv { hue: number; saturation: number; brightness: number }
export interface RunningGame { appId: string; name: string }
export interface GameProfile { exists: boolean; profile: Tdp; preset?: Preset }
export interface ModuleInfo { code: number | null; label: string; layout: string; status: string; connected: boolean }
export interface BatteryStatus { available: boolean; percent: number | null; status: string; seconds_to_full: number | null; power_w: number | null; source: "UPower" | "sysfs" | "none" }

export interface State {
  settings_error?: string;
  initializing?: boolean;
  startup_error?: string;
  supported: boolean;
  device: string;
  version: string;
  tdp_backend: string;
  tdp: Tdp;
  tdp_preset?: Preset;
  presets: Record<string, Tdp>;
  cpu_boost_supported: boolean;
  cpu_boost: boolean;
  controller: Controller;
  gpu_power_w: number | null;
  screen_installed: boolean;
  screen_conflict: boolean;
  edid_patched: boolean;
  edid_game_nits: number;
  button_fix_installed: boolean;
  charge_bypass_supported: boolean;
  charge_bypass: boolean;
  module_eject_supported: boolean;
  module_reset_supported: boolean;
  modules_reconnecting: boolean;
  modules_connected: boolean;
  module_left: ModuleInfo;
  module_right: ModuleInfo;
  tm_guard_enabled: boolean;
  tm_guard_status: string;
  tm_guard_recoveries: number;
  audio_fix_supported: boolean;
  audio_fix_enabled: boolean;
  audio_fix_installed: boolean;
  audio_profile: string;
  audio_fix_error: string;
  audio_calibration_available: boolean;
  audio_calibration_last: string;
}
