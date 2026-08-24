# Changelog

All notable changes to AYANEO 3 Companion, newest first.

## [1.0.1] - 2026-08-24

### Added

- A persistent CPU Boost switch on the TDP page, using Linux CPUFreq's
  kernel-wide boost control and restoring the saved choice after startup.

### Fixed

- AYANEO vendor-HID commands retry the complete transaction after an unanswered
  request instead of repeatedly waiting on a command that was already lost.
- A timed-out Magic Module eject leaves controller power enabled, preventing an
  incomplete latch movement from being interrupted by the power cut.
- Detect the proposed kernel `hid-ayaneo` sysfs interface and block competing
  direct-HID commands instead of racing the kernel driver.
- Keep a successfully changed OLED display definition enabled when gamescope's
  temporary EDID file cannot be normalized during the same RPC. The background
  monitor retries EDID normalization without reporting the switch as failed.
- Move display-definition removal off Decky's event loop and refuse to remove
  definitions not owned by AYANEO 3 Companion.

### Internal

- Backend tests up to 55 from 48, covering CPU Boost persistence, complete HID
  transaction retries, kernel-driver arbitration, safe Magic Module eject
  timeouts and OLED display-definition ownership.

## 1.0.0 - 2026-08-23

### Added

- Add an About page with the installed version and a secure GitHub release
  updater shared with the other Rayek Decky plugins.
- Show a live estimated time to full in Battery using UPower with a direct
  energy/power fallback. Battery polling runs only while that QAM page is open.
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

- Use the DXQ7D0023 panel's nominal RGB primaries with a D65 output white
  instead of an approximate Display-P3 description.
- Use AYANEO's advertised 800-nit maximum for PQ-to-Gamma-2.2 scaling and for
  the display metadata exposed to games.
- Replace the long accordion-style QAM with a compact hardware overview and a
  dedicated Decky-native page for each section. Every row now shows its current
  state before it is opened.
- Replace the modal TDP dropdown with in-page preset buttons so choosing a
  profile cannot close the section and Custom always reveals its sliders. The
  preset list, Custom action and per-game summary now follow LeGoTDP's layout.
- Keep Custom TDP changes behind an explicit Apply action and preserve the
  selected preset plus exact SPL, SPPT and FPPT values in per-game profiles.
- Use Decky's native confirmation dialogs for module ejection, module reset
  and speaker recalibration.
- Show action-specific loading indicators, inline result status and an RGB
  colour swatch instead of a raw hexadecimal value alone.
- Order the QAM sections as TDP, Vibration, RGB, Battery, Magic Modules, Audio
  and OLED Display.
- Use the same 500 ms pulse for firmware-strength changes and the vibration
  test button.
- Add a Minimum 5/8/10 W preset, change Max to 32/35/37 W and cap Custom SPPT
  and FPPT at 37 W.

### Fixed

- Recognize the AYANEO 3 EC's power-on value `0x00` as automatic charging,
  matching Linux 6.19's `ayaneo-ec` driver, and prefer its standard
  `charge_behaviour` ABI when a newer kernel provides it.
- Enforce the AYANEO 3 device boundary in every privileged hardware path and
  avoid device-specific EC/HID probes during unload on unsupported machines.
- Serialize TDP changes, per-game profile switches and charger/startup
  restoration so a delayed write cannot restore a stale power limit.
- Recover both speaker DSPs and PipeWire outputs after every failed audio
  tuning or recalibration transition.
- Upgrade only Companion-owned gamescope and InputPlumber files; never replace
  or remove another project's override at the same path.
- Preserve the active plugin page when Decky's dropdown or confirmation overlay
  temporarily hides QAM.
- Remove the yellow HDR cast caused by treating the bare module's
  customer-adjustable 0.300/0.310 white as the calibrated AYANEO output white.
- Stop treating the bridge's unverified 993-nit CTA value as proof that the
  bare module's 1000-nit HBM mode is active on the complete device.
- Keep action controls within the QAM panel by stacking their title, description
  and native full-width Decky button in Vibration, Magic Modules and Audio.
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
