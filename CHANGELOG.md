# Changelog

All notable changes to AYANEO 3 Companion, newest first.

## [1.3.0] - in development

This release brings LegionGo2Companion's design and safety work to AYANEO 3
Companion, including its TDP Control and Fan Control.

### TDP Control

- **New presets measured on the console:** Silent 10/15/20, Balanced 15/25/30
  and Performance 30/43/45 W (SPL/SPPT/FPPT). Performance uses the ceilings the
  SMU actually keeps; Custom goes from 5 W up to them. Minimum, Low power and Max
  are gone: Low power becomes Silent, the others keep their limits as Custom.
- **Game profiles like on the Legion:** per game, optionally with separate
  battery and charger values, and Global keeps a game's profile unused instead
  of deleting it.
- **CPU energy preference (EPP)** next to CPU Boost, globally or per game and
  charger state. Uninstall restores both.
- **Limits that stay put:** the firmware lowers SPL to 28 W whenever the charger
  is plugged in or unplugged; Companion now writes the limits again after every
  change and after sleep.
- RyzenAdj is kept outside the plugin folder, so updates no longer download it
  again. **Show details** shows the package power draw.

### Fan Control

- **Your own fan curve:** eight points from 0 to 100°C in the fan's own percent,
  three starting curves (Silent, Balanced, Aggressive) and a full-screen editor.
  The graph shows the live temperature and measured RPM. Below 10% the fan
  stops; the 100°C point never goes below 40%.
- **A curve per TDP preset**, if you want: Silent, Balanced, Performance and
  Custom can each use their own curve, others use the global one.
- **Safety from the Legion:** a separate watchdog returns the fan to the
  console's automatic curve if Companion stops, crashes or the console goes to
  sleep, and at 100°C sets full speed even if Companion is frozen. A fan that
  does not spin up or stop as asked turns Fan Control off with the reason.
- The fixed 60–100% manual speed and its systemd timer are gone. A manual speed
  left by an earlier version is returned to automatic.

### Fixes

- **TM button pressed by accident.** TM Guard now recognises the mode the TM
  button switches to and brings the controller back on its own in about ten
  seconds: it briefly cuts the controller's power, which also clears an arrow
  key that leaving TM mode could leave held. Before, it never noticed TM mode.
- **LC1 and RC1 work again after TM mode.** TM mode wiped their bindings; TM
  Guard now programs them again, and LC/RC no longer show up in Steam as
  LC1/RC1: all four buttons appear under their own names.
- **QAM with Fix Key Binding on.** The button table now sends the firmware's
  own QAM key; the previous one never opened the menu.
- **Colours after a SteamOS update.** The OLED display definition now lives in
  your `~/.config/gamescope/scripts`, which updates keep. Before, an update
  removed it from `/etc` and Companion put it back a moment after Gaming Mode had
  already started, so the screen stayed washed out until a restart.
- **Your lighting and vibration are kept on first install.** Companion shows
  what the controller already uses and changes nothing until you do. Uninstall
  puts back what it found.
- **L5/R5 after reinstalling.** Uninstall and Install Plugin from ZIP no longer
  leaves LC1/RC1 silent while the switch shows them enabled.
- The overview no longer shows 800 nits before the display has been read.

### Improvements

- **Restart Gaming Mode** from the OLED Display page when gamescope has not
  loaded the current definition yet.
- **Navigation like LegionGo2Companion:** icons on every row, the row you came
  from is selected again, and a page stays open when Quick Access closes or a
  menu opens. Status reads pause while the Companion tab is hidden.
- **Clearer stale data:** each row says how old its values are while a refresh
  is retried. RGB is now called Lighting.
- **Private diagnostic log** in `/var/log/ayaneo3companion`, with repeated
  messages limited to one a minute; Decky's own plugin logs are pruned to the
  newest ten.
- Helper programs no longer inherit Decky's bundled libraries, and Decky 3.2's
  stop hang no longer delays restoring the hardware.

## [1.2.0] - 2026-10-10

This update adds fan control and improves how Companion handles everyday
adjustments, game changes, and temporary hardware problems.

### New features

- **Fan Control:** see the fan's speed in RPM, keep the console's automatic
  fan curve, or choose a manual speed from 60% to 100%. Available when supported
  by the console's kernel.
- **Manual fan safety timer:** an independent timer restores automatic control
  if Companion stops responding. Stopping the plugin or resuming the console
  also returns the fan to Auto.

### Improvements

- **Faster startup and smoother menus.** Speaker checks, slow settings saves,
  and other background work are less likely to hold up unrelated controls.
- **Less waiting when navigating.** Reopening a page or quickly switching games
  no longer piles up repeated battery and profile checks. Companion refreshes
  the page or game you are currently using.
- **Clearer status messages.** Temporary read failures are shown alongside the
  last confirmed values. Messages clear after a successful refresh, while
  separate failed actions keep their own explanation.
- **More reliable downloads.** Update and hardware-helper downloads have time
  limits, and an update installs the exact version shown in the menu.
- **Broader kernel compatibility.** Companion uses newer AYANEO system controls
  when available and keeps the fallback needed by older SteamOS kernels.

### Fixes

#### TDP and game profiles

- Changing games quickly no longer lets the previous game's response replace
  the current game's preset or power limits.
- Saving or deleting a profile while reopening the menu no longer brings back
  the old preset or incorrectly re-enables Per Game Profile.
- Custom TDP values and the selected preset survive closing and reopening the
  menu for the same game. Failed preset changes also preserve unsaved edits.
- Rapid slider changes followed by Apply use the latest values, including the
  extra power allowed for short boosts.
- When PowerStation controls TDP, Companion explains why its own TDP controls
  are unavailable. Pending game profiles are retried when PowerStation releases
  control, and other hardware controls remain usable.
- A temporary profile lookup failure retries automatically. Its error clears
  after recovery without hiding an unrelated failed action.

#### Fan and battery

- Auto remains available during slow TDP or audio operations and after a
  temporary fan status failure. Auto status can also be read on kernels that
  do not allow reading manual speed while automatic control is active.
- Rapid manual fan changes and delayed responses no longer interfere with
  monitoring the latest setting. The safety timer can still restore Auto if
  Companion gets stuck, and retries when the fan interface is unavailable.
- Moving the manual fan slider and immediately pressing Apply uses the latest
  speed rather than the previous value.
- Battery read failures no longer leave a spinner running indefinitely or
  silently present old readings as current. Automatic refresh clears the error
  once readings are available again.
- Charging bypass is checked independently of TDP, including after plugging in
  a charger or resuming the console.

#### Controller, RGB, and vibration

- Fast changes to color, brightness, vibration strength, and vibration gain
  (FF Gain) no longer overwrite one another. The latest choices are kept when
  leaving the page.
- Failed controller changes restore the previous settings. Vibration tests
  wait for pending adjustments and do not run with unconfirmed values.
- Saved FF Gain is restored after startup, resume, or controller reconnection.
  Recovery waits for the built-in controller instead of using an external pad.
- Magic Modules recover their power, RGB, and vibration settings more reliably
  after removal, reinsertion, or startup with the controller powered off.
- Unavailable controller features are clearly marked and recover when the
  required interface returns.

#### Audio, display, and buttons

- Speaker setup no longer delays unrelated controls. Recalibration becomes
  available after both speakers are confirmed ready.
- The OLED fix and key-binding switches remember your choice and restore their
  system files when a SteamOS update replaces them.
- InputPlumber is reloaded only when its button mapping needs to change.

#### Installation, updates, and reliability

- The desktop installer handles folders with spaces and special characters.
- Installation finds the correct Decky folder, checks the full update package,
  and verifies that the required services start successfully.
- Failed installations restore the previous plugin and button mappings. If
  recovery cannot finish safely, the backup files are kept for repair.
- Incomplete update packages are rejected, and concurrent installations cannot
  replace each other's files.
- Settings and calibration tools better protect existing files from accidental
  replacement. Plugin shutdown also handles unfinished work more reliably.

### Quality checks

- Expanded regression tests cover rapid adjustments, navigation, game changes,
  temporary hardware failures, fan recovery, and failed installations.
- Automated checks cover Linux and Windows, frontend builds, dependencies, and
  install-package contents. Unused frontend dependencies were removed.

## [1.1.0] - 2026-09-06

### Fixed

- Commit complete settings atomically, retain a last-good recovery copy and
  refuse unsafe paths or unreadable settings instead of overwriting user intent.
- Keep hardware transactions serialized through cancellation and plugin shutdown,
  and restore the previous setting when a write or persistence step fails.
- Reject TDP and profile edits captured for a game that is no longer active.
- Preserve pending RGB and vibration changes when closing a page, merge rapid
  slider changes and prevent stale status replies from reverting saved controls.
- Keep native dropdown selections on their current page, retain confirmed
  overview and update results, and stop overlapping or hidden status polls.
- Clear delayed app notifications on unload and recover from temporary profile
  lookup errors without requiring the user to reopen the menu.
- Validate the exact GitHub release ZIP, its SHA-256 digest and archive contents;
  publish downloads atomically and avoid duplicate checks or downloads.
- Resolve audio-session and gamescope EDID paths from the Decky user account,
  including accounts with names or UIDs other than deck/1000.
- Bound EDID status reads and report a failed calibration-history save separately
  from a completed hardware calibration, retaining the recovery backup path.
- Verify hardware write readbacks, recover partial button-map changes and back
  off repeated interface failures while retaining resume and drift recovery.

### Internal

- Add frontend lifecycle regression tests and isolated backend failure tests.
- Check Python 3.10 on Linux, Python 3.13 on Windows, frontend types, the production
  build and package contents in GitHub Actions using pinned action revisions.
- Synchronize release metadata and add package payload validation. Existing
  functions, defaults, button mappings and the 35/37 W Custom limits are unchanged.

## [1.0.2] - 2026-08-25

### Fixed

- Verify AYANEO vendor-HID controller writes against the firmware's current
  report, retry stale writes and automatically repair RGB or vibration settings
  that drift after startup, resume or controller-device recreation.
- Restore FF_GAIN whenever the physical rumble input node is recreated, while
  excluding virtual and unrelated external controllers from device selection.
- Serialize RGB, firmware vibration and FF_GAIN changes in one frontend queue so
  rapid adjustments cannot overwrite one another with stale controller state.
- Keep Magic Module ejection isolated from background recovery and plugin unload,
  then restore controller settings after a real module reconnection without an
  unnecessary firmware save or mechanical reset.
- Recognize the original public Gamma 2.2 OLED definition as Companion-owned.
  Unknown existing definitions now require explicit replacement confirmation and
  are backed up and verified before the Companion definition is installed.

### Internal

- Backend tests up to 73 from 55, covering controller report parsing, drift and
  resume recovery, physical rumble-device selection, concurrent setting writes,
  Magic Module eject races and safe OLED definition replacement.

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
