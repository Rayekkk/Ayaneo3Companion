# Changelog

All notable changes to AYANEO 3 Companion, newest first.

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
