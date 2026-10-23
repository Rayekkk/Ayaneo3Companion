// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

export type Vibration = "off" | "low" | "medium" | "high";
export type RgbMode = "off" | "solid" | "pulse" | "rainbow";
export interface Controller { vibration: Vibration; ff_gain: number; rgb_mode: RgbMode; color: string; brightness: number }
export interface Hsv { hue: number; saturation: number; brightness: number }
export interface ModuleInfo { code: number | null; label: string; layout: string; status: string; connected: boolean }
export interface BatteryStatus { available: boolean; percent: number | null; status: string; seconds_to_full: number | null; power_w: number | null; source: "UPower" | "sysfs" | "none" }

export interface State {
  settings_error?: string;
  initializing?: boolean;
  startup_error?: string;
  supported: boolean;
  device: string;
  version: string;
  controller: Controller;
  controller_supported?: boolean;
  controller_error?: string;
  screen_installed: boolean;
  screen_conflict: boolean;
  screen_restart_needed?: boolean;
  screen_native?: boolean;
  screen_fix_enabled?: boolean;
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
