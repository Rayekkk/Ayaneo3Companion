# Changelog

All notable changes to AYANEO 3 Companion, newest first.

## [Unreleased]

### ✨ Added

- **About:** **Use Rayek's store** makes Rayek's store Decky's store channel after you confirm, so Decky's Store
  tab lists Rayek's plugins and offers their updates.

### 🔧 Changed

**Panel**
- The B button on a module page goes back to the module list instead of closing Quick Access, with focus
  on the module you came from.
- Leaving Fan Control or TDP with unsaved curve or Custom limit changes first asks "Discard changes?".
- Labels and titles use sentence case and US English (for example **Key binding**, **Bypass charging**,
  "color").
- Loading shows a spinner, and a failed first TDP read offers **Try again**.
- Confirmations ask a question ("Eject the left module?") and report their result in a notification.

**About**
- Shows version, author and included modules, with **Updates** at the bottom using the same texts as the
  other plugins.

**Updates**
- A downloaded ZIP is shown as `~/Downloads/…`, and if the plugin cannot start, the updater stays
  available.
- A release ZIP is fetched only from GitHub's own download servers, and an incomplete release names the
  file it is missing.
- A newer release that cannot be offered now says why instead of only showing its version.

**Fan**
- The curve graph uses the editor shared with the other plugins and is edited the same way (A or a tap,
  LB/RB, D-pad, dragging); B now reads "Done".

**Errors**
- When something fails unexpectedly, the panel shows a short message and the details go to the Decky log,
  never file paths.

**Downloads**
- RyzenAdj, the speaker calibration file and plugin updates are downloaded by a separate helper with
  a time limit, so a stalled connection can no longer hold up the plugin.
- RyzenAdj's download no longer uses the RAM-backed `/tmp`.

### 🐛 Fixed
- **Settings:** an empty settings file no longer blocks every change as lost settings.
- **Settings:** a damaged settings file is kept as `.corrupt-<time>` before it is replaced.
- **Settings:** settings that could not be read back are never written, and recovery copies are as private as the
  settings themselves.

## [1.3.0] - 2026-10-24

### ✨ Added

**TDP**
- Game profiles like on the Legion, per game and optionally with separate battery and charger values.
- Global keeps a game's profile unused instead of deleting it.
- CPU energy preference (EPP) next to CPU Boost, globally or per game and charger state; Uninstall restores
  both.

**Fan**
- Your own fan curve with eight points from 0 to 100°C in the fan's own percent, three starting curves
  (Silent, Balanced, Aggressive) and a full-screen editor.
- The graph shows the live temperature and measured RPM; below 10% the fan stops, and the 100°C point never
  goes below 40%.
- Optionally a curve per TDP preset: Silent, Balanced, Performance and Custom can each use their own, others
  use the global one.
- A separate watchdog returns the fan to the console's automatic curve if Companion stops, crashes or the
  console goes to sleep, and sets full speed at 100°C even if Companion is frozen.
- A fan that does not spin up or stop as asked turns Fan Control off and shows the reason.

### 🔧 Changed

**TDP**
- New presets measured on the console: Silent 10/15/20, Balanced 15/25/30 and Performance 30/43/45 W
  (SPL/SPPT/FPPT); Custom goes from 5 W up to the Performance ceilings.
- Minimum, Low power and Max are gone: Low power becomes Silent, the others keep their limits as Custom.
- The firmware lowers SPL to 28 W whenever the charger is plugged in or unplugged, so Companion now writes
  the limits again after every change and after sleep.
- RyzenAdj is kept outside the plugin folder, so updates no longer download it again.
- **Show details** shows the package power draw.

**Display**
- **Restart Gaming Mode** on the OLED Display page when gamescope has not loaded the current definition
  yet.

**Panel**
- Navigation like LegionGo2Companion: icons on every row, the row you came from is selected again, and a
  page stays open when Quick Access closes or a menu opens.
- Status reads pause while the Companion tab is hidden.
- Each row says how old its values are while a refresh is retried.

**Lighting**
- RGB is now called Lighting.

**Logs**
- A private diagnostic log in `/var/log/ayaneo3companion` limits repeated messages to one a minute, and
  Decky's own plugin logs are pruned to the newest ten.

**Plugin**
- Helper programs no longer inherit Decky's bundled libraries.
- Decky 3.2's stop hang no longer delays restoring the hardware.
- The background monitors no longer rescan every device twice a second, and the controller's memory is
  written once per change.

### 🐛 Fixed

**TM Guard**
- After an accidental TM button press it now recognises the mode and brings the controller back in
  about ten seconds by briefly cutting its power; before, it never noticed TM mode.
- The power cut also clears an arrow key that leaving TM mode could leave held.
- After three tries it asks you to press TM until the light is white instead of restarting the
  controller again and again.
- A USB keyboard with the same chip no longer sets it off.

**Buttons**
- LC1 and RC1 work again after TM mode, because TM Guard programs their bindings again.
- LC/RC no longer show up in Steam as LC1/RC1; all four buttons appear under their own names.
- With Fix Key Binding on, the button table sends the firmware's own QAM key; the previous one never
  opened the menu.
- Uninstall and Install Plugin from ZIP no longer leave LC1/RC1 silent while the switch shows them
  enabled.

**Display**
- The OLED display definition now lives in `~/.config/gamescope/scripts`, which SteamOS updates keep;
  before, an update removed it and the screen stayed washed out until a restart.
- Turning the display definition off now asks for the Gaming Mode restart it needs and puts back a
  definition Companion had replaced.
- The HDR fix can be switched on again while SteamOS's own display profile is in use.
- The overview no longer shows 800 nits before the display has been read.

**Lighting**
- Your lighting and vibration are kept on first install: Companion shows what the controller already
  uses and changes nothing until you do; Uninstall puts back what it found.

**Install**
- Speaker-calibration and display backups move to `/home/.ayaneo3companion-backups`, so they survive
  SteamOS updates.

**Audio**
- Speaker tuning in the background never interrupts playback and stops retrying when the tuning does not
  take.

**TDP**
- A change right after plugging in the charger no longer leaves 28 W.
- A CPU Boost change during a profile switch no longer loses the game's profile.
- CPU Boost and EPP are left to PowerStation while it runs.
- **Retry** appears when the RyzenAdj helper breaks while running.

**Fan**
- Fan Control no longer takes the fan from another fan tool at startup.
- An edited curve can still be saved after Fan Control turned off on an error.

**Battery**
- Uninstall turns charge bypass off whenever the hardware has it on, even when the saved settings say
  otherwise.

**Magic Modules**
- **Reset Magic Modules** works again after a module was pushed back in after Eject.

### 🗑️ Removed
- **Fan:** the fixed 60–100% manual speed and its systemd timer; a manual speed left by an earlier version is returned
  to automatic.

## [1.2.0] - 2026-10-10

### ✨ Added
- **Fan:** see the fan's speed in RPM, keep the console's automatic curve, or choose a manual speed from 60% to
  100%, when supported by the console's kernel.
- **Fan:** an independent timer restores automatic control if Companion stops responding; stopping the plugin or
  resuming the console also returns the fan to Auto.

### 🔧 Changed

**Panel**
- Faster startup and smoother menus; speaker checks, slow settings saves and other background work are
  less likely to hold up unrelated controls.
- Reopening a page or quickly switching games no longer piles up repeated battery and profile checks;
  Companion refreshes the page or game you are currently using.
- Temporary read failures are shown alongside the last confirmed values and clear after a successful
  refresh, while separate failed actions keep their own explanation.

**Downloads**
- Update and hardware-helper downloads have time limits.

**Updates**
- An update installs the exact version shown in the menu.

**Plugin**
- Companion uses newer AYANEO system controls when available and keeps the fallback needed by older
  SteamOS kernels.

### 🐛 Fixed

**TDP**
- Changing games quickly no longer lets the previous game's response replace the current game's preset or
  power limits.
- Saving or deleting a profile while reopening the menu no longer brings back the old preset or incorrectly
  re-enables Per Game Profile.
- Custom values and the selected preset survive closing and reopening the menu for the same game, and failed
  preset changes preserve unsaved edits.
- Rapid slider changes followed by Apply use the latest values, including the extra power allowed for short
  boosts.
- When PowerStation controls TDP, Companion explains why its own TDP controls are unavailable, retries
  pending game profiles when control is released, and keeps other hardware controls usable.
- A temporary profile lookup failure retries automatically, and its error clears after recovery without
  hiding an unrelated failed action.

**Fan**
- Auto remains available during slow TDP or audio operations and after a temporary fan status failure, and
  its status can be read on kernels that do not allow reading manual speed in automatic mode.
- Rapid manual fan changes and delayed responses no longer interfere with monitoring the latest setting.
- The safety timer can still restore Auto if Companion gets stuck, and retries when the fan interface is
  unavailable.
- Moving the manual fan slider and immediately pressing Apply uses the latest speed.

**Battery**
- Read failures no longer leave a spinner running indefinitely or silently present old readings as
  current; the error clears once readings are available again.
- Charging bypass is checked independently of TDP, including after plugging in a charger or resuming
  the console.

**Lighting**
- Fast changes to color, brightness, vibration strength and FF Gain no longer overwrite one another,
  and the latest choices are kept when leaving the page.

**Controller**
- Failed controller changes restore the previous settings, and vibration tests wait for pending
  adjustments and never run with unconfirmed values.
- Saved FF Gain is restored after startup, resume or controller reconnection, waiting for the
  built-in controller instead of using an external pad.
- Unavailable controller features are clearly marked and recover when the required interface
  returns.

**Magic Modules**
- Power, RGB and vibration settings are recovered more reliably after removal, reinsertion, or
  startup with the controller powered off.

**Audio**
- Speaker setup no longer delays unrelated controls, and recalibration becomes available after both
  speakers are confirmed ready.

**Display**
- The OLED fix and key-binding switches remember your choice and restore their system files when a
  SteamOS update replaces them.

**Buttons**
- InputPlumber is reloaded only when its button mapping needs to change.

**Install**
- The desktop installer handles folders with spaces and special characters.
- Installation finds the correct Decky folder, checks the full update package and verifies that the
  required services start.
- Failed installations restore the previous plugin and button mappings, and if recovery cannot finish
  safely, the backup files are kept for repair.

**Updates**
- Incomplete update packages are rejected, and concurrent installations cannot replace each other's
  files.

**Settings**
- Settings and calibration tools better protect existing files from accidental replacement.

**Plugin**
- Shutdown handles unfinished work more reliably.

## [1.1.0] - 2026-09-06

### 🐛 Fixed

**Settings**
- Complete settings are committed atomically with a last-good recovery copy, and unsafe paths or
  unreadable settings are refused instead of overwriting your choices.

**Controller**
- Hardware writes stay serialized through cancellation and plugin shutdown, and the previous setting
  is restored when a write or save step fails.
- Hardware write readbacks are verified, partial button-map changes are recovered, and repeated
  interface failures back off while resume and drift recovery stay in place.

**TDP**
- Edits captured for a game that is no longer active are rejected.

**Lighting**
- Pending RGB and vibration changes are preserved when closing a page, rapid slider changes are
  merged, and stale status replies no longer revert saved controls.

**Panel**
- Native dropdown selections stay on their current page, confirmed overview and update results are
  retained, and overlapping or hidden status polls stop.
- Delayed app notifications are cleared on unload, and temporary profile lookup errors recover without
  reopening the menu.

**Updates**
- The exact GitHub release ZIP is validated, including its SHA-256 digest and archive contents;
  downloads are published atomically and duplicate checks or downloads are avoided.

**Audio**
- Audio-session and gamescope EDID paths are resolved from the Decky user account, including accounts
  with names or UIDs other than deck/1000.

**Display**
- EDID status reads are bounded, and a failed calibration-history save is reported separately from a
  completed hardware calibration, keeping the recovery backup path.

## [1.0.2] - 2026-08-25

### 🐛 Fixed

**Controller**
- AYANEO vendor-HID writes are verified against the firmware's current report and retried when
  stale.
- FF_GAIN is restored whenever the physical rumble input node is recreated, and virtual or unrelated
  external controllers are ignored.

**Lighting**
- RGB or vibration settings that drift after startup, resume or controller-device recreation are
  repaired automatically.
- RGB, firmware vibration and FF_GAIN changes go through one queue, so rapid adjustments cannot
  overwrite one another with stale controller state.

**Magic Modules**
- Ejection is isolated from background recovery and plugin unload, and controller settings are
  restored after a real module reconnection without an unnecessary firmware save or mechanical reset.

**Display**
- The original public Gamma 2.2 OLED definition is recognized as Companion-owned.
- Unknown existing definitions now require explicit replacement confirmation and are backed up and
  verified before the Companion definition is installed.

## [1.0.1] - 2026-08-24

### ✨ Added
- **TDP:** a persistent CPU Boost switch on the TDP page, restored after startup.

### 🐛 Fixed

**Controller**
- AYANEO vendor-HID commands retry the complete transaction after an unanswered request instead of
  repeatedly waiting on a command that was already lost.
- The proposed kernel `hid-ayaneo` sysfs interface is detected, and competing direct-HID commands are
  blocked instead of racing the kernel driver.

**Magic Modules**
- A timed-out eject leaves controller power enabled, so an incomplete latch movement is not
  interrupted by the power cut.

**Display**
- A successfully changed OLED display definition stays enabled when gamescope's temporary EDID file
  cannot be normalized at the same time; the background monitor retries without reporting the switch as failed.
- Display-definition removal no longer blocks Decky, and definitions not owned by AYANEO 3 Companion are
  not removed.

## [1.0.0] - 2026-08-23

### ✨ Added

**About**
- A page with the installed version and a secure GitHub release updater shared with the other Rayek Decky
  plugins.

**Battery**
- A live estimated time to full, polled only while that page is open.

**Magic Modules**
- Every known left and right module is identified, with its reported module ID, physical
  upper/lower layout and partial connection state.
- Quick Reset with automatic restoration of RGB, vibration, FF_GAIN and optional LC1/RC1
  mappings.

**TM Guard**
- Enabled by default, it detects an accidental hardware mode switch and restores custom gamepad mode
  plus the Companion configuration.

**Controller**
- A persistent Linux FF_GAIN slider in 10% steps below the firmware vibration-strength slider.

**Buttons**
- One Fix Key Binding switch that programs LC1/RC1 in the firmware, maps them to L5/R5 and keeps LC/RC
  as L4/R4.

### 🔧 Changed

**Display**
- Uses the DXQ7D0023 panel's nominal RGB primaries with a D65 output white instead of an approximate
  Display-P3 description.
- Uses AYANEO's advertised 800-nit maximum for PQ-to-Gamma-2.2 scaling and for the display metadata
  exposed to games.

**Panel**
- The long accordion-style menu is replaced by a compact hardware overview and a dedicated page for each
  section, with every row showing its current state before it is opened.
- Sections are ordered TDP, Vibration, RGB, Battery, Magic Modules, Audio and OLED Display.
- Action-specific loading indicators, inline result status and an RGB color swatch replace a raw
  hexadecimal value alone.
- Decky's native confirmation dialogs are used for module ejection, module reset and speaker
  recalibration.

**TDP**
- The modal dropdown is replaced by in-page preset buttons, so choosing a profile cannot close the section
  and Custom always reveals its sliders; the layout follows LeGoTDP.
- Custom changes stay behind an explicit Apply, and per-game profiles keep the selected preset plus exact
  SPL, SPPT and FPPT values.
- Added a Minimum 5/8/10 W preset, Max is now 32/35/37 W, and Custom SPPT and FPPT are capped at 37 W.

**Controller**
- Firmware-strength changes and the vibration test button use the same 500 ms pulse.

### 🐛 Fixed

**Battery**
- The AYANEO 3 EC's power-on value `0x00` is recognized as automatic charging, and the standard
  `charge_behaviour` interface is preferred when a newer kernel provides it.
- Charge-bypass reads and writes are serialized with every other AYANEO EC access.

**Install**
- The AYANEO 3 device check applies to every privileged hardware path, and device-specific probes are
  skipped during unload on unsupported machines.
- Only Companion-owned gamescope and InputPlumber files are upgraded; another project's override at the
  same path is never replaced or removed.

**TDP**
- TDP changes, per-game profile switches and charger/startup restoration are serialized, so a delayed write
  cannot restore a stale power limit.

**Audio**
- Both speaker DSPs and PipeWire outputs recover after every failed tuning or recalibration.

**Panel**
- The active plugin page is preserved when Decky's dropdown or confirmation overlay temporarily hides
  Quick Access.
- Action controls in Vibration, Magic Modules and Audio stay within the panel, with title, description
  and a full-width Decky button stacked.
- The refresh no longer waits on shared plugin state while reading sysfs, gamescope files and the
  embedded controller.

**Display**
- The yellow HDR cast is gone; it came from treating the bare module's adjustable 0.300/0.310 white as
  the calibrated output white.
- The bridge's unverified 993-nit value is no longer taken as proof that the 1000-nit HBM mode is
  active.

**Magic Modules**
- The `0x88` activation reset is no longer issued during every boot, TM Guard recovery and
  ordinary module reconnection; LC1/RC1 bindings already persist in the controller.
- A manually reinserted module is detected even when controller power stayed on, and RGB,
  vibration and FF_GAIN are restored once vendor HID is ready.

**Buttons**
- InputPlumber restarts no longer block Decky's event loop.
- The old 8840U-only InputPlumber device override is no longer installed on the HX 370 model; native F24
  and legacy Meta+D QAM inputs coexist, and only the event from the detected firmware variant is active.
- The complete 33-slot AYANEO button table is initialized, activated with a controller configuration
  reset and followed by restoring RGB/vibration; writing only the two LC1/RC1 slots left the table inactive.
- The aya7 override is installed as `/etc/inputplumber/capability_maps.d/ayaneo_type7.yaml`; the
  previous `01-` name was loaded first and overwritten by the stock map.
- The `custom mode required` status is read from byte 18, as on the captured HX 370 response, so rear
  buttons are no longer silently left disabled.

**Controller**
- The UI recovers after a failed vendor-HID write, and RGB slider commits are debounced without an
  unnecessary write when the page opens.

**Plugin**
- All background monitors are cancelled together on unload, so Decky does not have to terminate the old
  plugin after its five-second stop deadline.
