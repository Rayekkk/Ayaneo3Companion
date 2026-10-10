# Changelog

All notable changes to AYANEO 3 Companion, newest first.

## [Unreleased]

### Fixed

- Keep settings recovery-status checks and game-profile reads from blocking
  Decky's event loop during slow disk commits; fan Auto stays independent of
  persisted-settings I/O.
- Verify raw-EC controller power writes and restore the previous register on
  failure, matching the native kernel path's confirmation and boolean validation.
- Show failed status refreshes alongside the last confirmed values and clear
  the message after a fresh successful read, including on individual control pages.
- Treat missing or timed-out PowerStation ownership tools as a pre-write
  capability rejection, avoiding a redundant hardware rollback and repeated
  slow service probes. Failures after a RyzenAdj write still restore old limits.
- Pass the Desktop installer's location as a separate argument, preserving
  spaces, apostrophes and shell metacharacters without evaluating path contents.
  Accept local file URIs and retain the bootstrap's exit status.
- Merge rapid Custom TDP slider events before rendering and apply the current
  draft, including current boost headroom. Preserve unapplied drafts after a
  failed preset and ignore stale slider callbacks during an active apply.
- Retain the last vibration strength and FF Gain choice when quick slider
  movement returns to the original value before the next render.
- Apply the latest manual fan speed even when Apply is pressed immediately
  after the slider changes.
- Discover and verify speaker controls in the background without delaying
  startup of unrelated controls. Retry late ALSA discovery for disabled tuning
  without enabling it, and expose calibration only after DSP verification.
- Open settings for reading and permission hardening without waiting on a
  substituted FIFO; continue rejecting non-regular descriptors before use.
- Create offline calibration candidates exclusively, preserving files created
  by another process and refusing dangling output symlinks.
- Preserve cancellation after a drained worker fails, preventing a monitor
  retry from swallowing shutdown and keeping unload pending indefinitely.
- Block three-limit TDP writes when PowerStation is active or starting; its
  two-property API cannot enforce or verify the plugin's FPPT ceiling.
- Commit combined controller firmware/RGB and FF Gain edits in one transaction,
  restoring both devices on failure.
- Accept TDP state only for the acknowledged active game so an earlier game's
  poll cannot replace the next game's selected preset.
- Report missing or kernel-owned vendor HID controls as unavailable, including
  module eject/reset, with recovery when the interface returns.
- Recover module presence and power through EC before requiring vendor HID,
  including eject/reinsert and startup with the controller powered off.
- Retry the active game's pending TDP profile after PowerStation releases
  control, and keep device diagnostics and independent controls visible when
  game-profile synchronization fails.
- Preserve both the Custom preset selection and its unsaved values when QAM
  is closed and reopened for the same game.
- Restore saved evdev FF Gain at startup, after resume and input-device
  recreation even when vendor HID is unavailable or owned by `hid-ayaneo`.
  Retry failed gain writes independently and pause restoration during module
  operations.
- Require a known internal AYANEO physical path for FF Gain and vibration tests;
  wait for its evdev node instead of selecting a sole external Xbox-compatible pad.
- Acknowledge successful settings writes without repeating unrelated service
  diagnostics inside the serialized action queue. Retain observed capabilities
  and refresh them through regular status reads without replacing newer settings.
- Detect game changes during a pending profile RPC and immediately send the
  latest queued context when it completes. Reuse in-flight fan reads when opening
  the fan page and immediately retry a stale read after reopening QAM.
- Require matching custom-mode HID readback before confirming controller writes
  or recovery; retry one missing or mismatched response and roll back failed edits.
- Bound RyzenAdj and audio-calibration downloads by elapsed transfer time as well
  as size, with incremental reads and shorter individual network timeouts.
- Leave pending TDP retries to the power monitor so same-game heartbeats cannot
  repeat slow ownership probes inside the serialized settings queue.
- Merge fast write acknowledgements only into their own controls, preserving
  fresher observations of unrelated controls and device diagnostics.
- Ignore unreadable unrelated hwmon names when finding the AYANEO fan, including
  in the independent Auto recovery guard; still reject ambiguous AYANEO devices.
- Return same-game heartbeats before the TDP monitor's transaction lock and
  serialize charge control independently, keeping unrelated settings responsive.
- Accept global control acknowledgements across game changes while preserving
  strict context checks for TDP and per-game profile edits.
- Flush and confirm the complete controller queue before a vibration test;
  skip tests after failed writes or unmount, refresh restored settings immediately
  after failure and disable FF Gain while an action is in progress.
- Send only edited controller fields and merge them with the latest confirmed
  settings inside the backend transaction, preserving pending changes across
  panel remounts. Brightness changes no longer replace color or vibration.
- Observe PowerStation ownership in one background status task, reusing its
  result for five seconds between probes so slow service diagnostics do not
  delay startup or unrelated status fields. TDP writes still check live ownership.
- Discard ownership observations from before or during a newer TDP write, or
  another backend lifetime. Publish capability changes from live TDP writes
  immediately and drain pending diagnostics during shutdown.
- Preserve failed controller fields across a queue drain, so a successful
  unrelated patch cannot start a vibration test with unconfirmed settings.
  A confirmed newer edit of the same field can supersede its earlier failure.
- Serialize fan commands separately from persistent settings transactions,
  keeping Auto available during slow TDP or audio operations while retaining
  hardware validation, write admission and shutdown tracking.
- Avoid starting a fan monitor when a manual write completes during shutdown;
  drain the admitted write and restore Auto without leaving a late task.
- Monitor charge bypass independently of TDP retries and downloads, preserving
  periodic verification and prompt checks after charger changes or resume.
- Discover the bootstrap install directory from Decky's service configuration
  and restore the complete previous plugin, owned maps and service state after
  an installation failure.
- Match Decky's privileged-directory fallbacks, exclude concurrent installers,
  verify restarted services before removing backups and retain the live
  deployment if rollback cannot stop the Loader.
- Bind update downloads to the version displayed in the panel and reject ZIPs
  missing any required runtime module or hardware asset.
- Replace the obsolete build dependency chain and check npm advisories in CI.

### Internal

- Add event-loop regressions for slow settings commits, raw-EC power readback
  and rollback faults, pre-write ownership failures and post-write TDP recovery.
  Exercise cached status-read failures, recovery and obsolete poll completions.
- Run the shipped Desktop launcher with harmless temporary bootstrap scripts
  for special paths, local URIs, rejected remote locations and failed installs.
- Exercise batched slider/apply events, failed-preset draft recovery, 300
  interleaved TDP changes, audio startup and shutdown, late disabled-audio
  discovery and speaker readiness UI. Add POSIX child-process FIFO regressions.
- Cover failed partial-write queues, vibration-test admission, independent fan
  operations and their ordering, late fan-monitor startup, independent charge
  drift recovery, charger/resume scheduling, retry pacing and monitor shutdown.
- Cover partial controller commits and rollback, panel remount ordering,
  blocked service probes, diagnostic expiry, TDP observation races, startup
  responsiveness and diagnostic shutdown on isolated hardware/service doubles.
- Add regression tests for controller transactions, game-state ordering, HID
  availability, installer rollback and incomplete archives; validate the built
  package with the updater's archive checks.
- Cover module power recovery, pending game profiles, profile-error diagnostics,
  Custom draft reopening, installer locking and service verification failures.
- Cover independent FF Gain recovery, missing input devices, failed writes,
  module-operation races and monitor shutdown.
- Cover actual rumble-device selection and event writes on temporary files,
  blocked diagnostics, fast write acknowledgements, game-transition coalescing
  and fan-page refresh ordering.
- Cover missing HID confirmation, recovery readback, transfer deadlines,
  heartbeat retry ownership, unrelated state preservation and fan discovery
  across disappearing hwmon entries.
- Cover blocked TDP monitors without delaying charge/heartbeat/RGB actions,
  charge restoration after commit, global acknowledgements during game changes
  and vibration tests with pending, failed or interrupted controller writes.

## [1.2.0] - 2026-09-24

### Added

- Add native AYANEO EC fan status and guarded Auto/manual fan control when the
  kernel exposes the required hwmon interface.

### Fixed

- Prefer the native `ayaneo-ec` controller power and module-presence controls on
  newer kernels while retaining the raw EC fallback on SteamOS 3.8/kernel 6.16.
- Save the OLED and key-binding switches and restore their system definitions
  after SteamOS replaces `/etc`; reload InputPlumber only when its map changes.
- Keep the fan recovery action available after a temporary status failure and
  retry independent Auto restoration across longer EC outages.
- Keep Auto fan status available when the EC rejects a manual PWM read; continue
  to require PWM readback before confirming manual control.

### Internal

- Split the hardware overview into focused pages and extend regression coverage
  for kernel compatibility, startup recovery, fan safety and QAM navigation.
- Drop the unused React Icons package from the frontend and release payload.

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
