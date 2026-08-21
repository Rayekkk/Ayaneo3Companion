# Changelog

All notable changes to AYANEO 3 Companion, newest first.

## Unreleased

### Added

- Identify every known left and right AYANEO 3 Magic Module, including its
  reported module ID, physical upper/lower layout and partial connection state.
- Add Magic Module Quick Reset with automatic restoration of RGB, vibration,
  FF_GAIN and optional LC1/RC1 mappings.
- Add TM Guard, enabled by default, which detects an accidental hardware mode
  switch and restores custom gamepad mode plus the Companion configuration.
- Add a persistent Linux FF_GAIN slider in 10% steps below the controller's
  firmware vibration-strength slider.
- Add one Fix Key Binding switch that programs LC1/RC1 in the AYANEO firmware,
  maps them to L5/R5 and preserves LC/RC as L4/R4.

### Changed

- Replace the long accordion-style QAM with a compact hardware overview and a
  dedicated Decky-native page for each section. Every row now shows its current
  state before it is opened.
- Use Decky's native dropdown for TDP presets and confirmation dialogs for
  module ejection, module reset and speaker recalibration.
- Show action-specific loading indicators, inline result status and an RGB
  colour swatch instead of a raw hexadecimal value alone.
- Order the QAM sections as TDP, Vibration, RGB, Battery, Magic Modules, Audio
  and OLED Display.
- Use the same 500 ms pulse for firmware-strength changes and the vibration
  test button.
- Add a Minimum 5/8/10 W preset, change Max to 32/35/37 W and cap Custom SPPT
  and FPPT at 37 W.

### Fixed

- Stop issuing the Magic Module `0x88` activation reset during every boot, TM
  Guard recovery and ordinary module reconnection. LC1/RC1 bindings already
  persist in controller NVRAM through `AYA_SAVE`.
- Do not hold the plugin's shared state lock while the QAM refresh reads sysfs,
  gamescope files and the embedded controller.
- Serialize charge-bypass reads and writes with every other AYANEO EC access,
  and restart InputPlumber outside Decky's asynchronous event loop.
- Recover controller UI state after a failed vendor-HID write and debounce RGB
  slider commits without issuing an unnecessary write when the page opens.
- Cancel all background monitors together during unload so Decky does not have
  to terminate the old plugin process after its five-second stop deadline.
- Detect a manually reinserted Magic Module even when controller power stayed
  on, then retry restoring RGB, vibration and FF_GAIN until vendor HID is ready.
- Stop installing the old 8840U-only full InputPlumber device override on the
  HX 370 model. Native F24 and legacy Meta+D QAM inputs coexist declaratively,
  so only the event emitted by the detected firmware variant becomes active.
- Initialize the complete 33-slot AYANEO button table, activate it with the
  controller configuration reset and restore current RGB/vibration. Writing
  only the two LC1/RC1 slots was acknowledged but left the table inactive.
- Install the aya7 override as `/etc/inputplumber/capability_maps.d/ayaneo_type7.yaml`.
  InputPlumber sorts maps by filename, so the previous `01-` name was loaded
  before and then overwritten by the stock map with the same `aya7` ID.
- Read the AYANEO `custom mode required` status from byte 18, matching HHD and
  the captured HX 370 response. Byte 17 is zero on this revision, so the old
  check skipped `AYA_CUSTOM` and firmware silently left rear buttons disabled.

## 0.6.1

### Fixed

- Write each EFI calibration update as one complete `write(2)` request and do
  not call unsupported `fsync(2)` on efivarfs. The previous call returned
  `EINVAL` after a successful write and incorrectly triggered rollback.
- Report the required full restart after saving calibration instead of trying
  to override the driver's cached calibration values directly.

## 0.6.0

### Added

- Guarded two-step speaker recalibration for both CS35L41 amplifiers, available
  only while the AYANEO tuning is installed and active.
- Validation, readback and an on-device backup before saving new calibration
  values to the existing Cirrus EFI variable.

### Fixed

- Temporarily release the physical ALSA PCM before reloading smart-amp DSPs,
  then restore PipeWire automatically. Audio Fix no longer waits on an active
  Steam stream before failing with `Device or resource busy`.
- Persist the Audio Fix switch only after both amplifier profiles were changed
  and verified successfully.

## 0.5.0

### Added

- Automatic CS35L41 audio fix that loads the official AYANEO 3 v0.65 tuning
  for both speakers instead of the generic Cirrus v0.58 fallback.
- Collapsible Audio section with status, enable/disable control and a safe DSP
  reapply action that does not remove the ALSA sound card.

### Fixed

- Wait for active playback to finish before switching smart-amp firmware and
  return a clear error instead of leaving either channel disabled.
- Run host `systemctl` without Decky's bundled library path, fixing QAM button
  map restarts on current SteamOS builds.

## 0.4.4

### Added

- Standalone `bootstrap.sh` installer and a Desktop Mode launcher for fresh
  systems where QAM is not yet accessible. They install the plugin and enable
  its InputPlumber mapping.

## 0.4.3

### Removed

- Removed the experimental shutdown LED hook because it could leave controller
  power disabled after boot. Controller power is no longer changed at shutdown.

## 0.4.2

### Fixed

- Turn off controller power and joystick LEDs during a real system shutdown
  using a dedicated systemd hook. Decky reloads do not trigger the hook.

## 0.4.1

### Fixed

- Map the AYANEO 3's cold-boot `F24` QAM button event to Steam Quick Access,
  while retaining compatibility with the alternate `Meta+D` firmware mapping.

## 0.4.0

### Added

- Magic Module controls to eject the left, right or both controller modules.
- Automatic controller power-off after release and power-on after both modules
  are seated again, matching the complete HHD replacement sequence.

## 0.3.1

### Changed

- Reduced the Max preset boost limits to a safer 35 W SPL, 38 W SPPT and
  40 W FPPT. Custom tuning still permits 35/40/45 W.

## 0.3.0

### Added

- Battery charge bypass for AYANEO 3. The setting writes the vendor EC charge
  inhibit value, verifies the actual register state and persists across reboots.
- Automatic charging is restored when the plugin is uninstalled.

## 0.2.9

### Added

- Max preset with 35 W SPL, 40 W SPPT and 45 W FPPT.
- Charger monitoring that re-applies the active global or per-game TDP several
  times over six seconds after connecting or disconnecting external power.

## 0.2.8

### Changed

- Custom SPPT now reaches 40 W and FPPT reaches 45 W. Both values were accepted
  by the Ryzen 7 8840U SMU on retail AYANEO 3 hardware; sustained SPL remains
  capped at AYANEO's official 35 W limit.

## 0.2.7

Initial public source version.

### Added

- TDP presets, custom SPL/SPPT/FPPT limits and per-game profiles.
- PowerStation support on Bazzite and a verified RyzenAdj fallback on SteamOS.
- Controller vibration strength, a test button and joystick-ring RGB controls.
- Persistent Steam, Quick Access, LC1/L4 and RC1/R4 button mappings.
- AYANEO 3 OLED gamescope definition and 800 nit EDID correction.

### Fixed

- Start the Steam application watcher with the plugin so running games are
  detected with the QAM open or closed.
