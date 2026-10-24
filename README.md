<div align="center">

<img src="https://raw.githubusercontent.com/Rayekkk/Ayaneo3Companion/main/Ayaneo3Companion.png" alt="AYANEO 3 Companion" width="760">

[![Release](https://img.shields.io/github/v/release/Rayekkk/Ayaneo3Companion?style=for-the-badge&label=release&color=C2410C&labelColor=141417)](https://github.com/Rayekkk/Ayaneo3Companion/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Rayekkk/Ayaneo3Companion/total?style=for-the-badge&label=downloads&color=15803D&labelColor=141417)](https://github.com/Rayekkk/Ayaneo3Companion/releases)
[![Device](https://img.shields.io/badge/device-AYANEO_3_OLED-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/github/license/Rayekkk/Ayaneo3Companion?style=for-the-badge&label=license&color=424A53&labelColor=141417)](LICENSE)

**The missing AYANEO 3 hardware controls in one Steam overlay panel.**
TDP profiles, smart-amp audio, battery bypass, Magic Modules, fan control, vibration, lighting, rear buttons and OLED display support.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [How it works](#how-it-works) · [Development](#development)

</div>

---

## Features

| | |
|---|---|
| **TDP Control** | Silent 10/15/20, Balanced 15/25/30 and Performance 30/43/45 W presets, ported from LegionGo2Companion |
| **Custom TDP** | SPL 5–30 W, SPPT up to 43 W and FPPT up to 45 W, as one dial or with separate boost limits |
| **Per-game profiles** | Per game, optionally with separate battery and charger settings; a profile can be kept unused |
| **CPU Boost and EPP** | CPU Boost and the CPU energy preference, globally or per game and charger state |
| **Live power** | Shows the processor package's power draw on the TDP page |
| **Charge bypass** | Powers the console from its charger while inhibiting battery charging |
| **Fan control** | Your own eight-point fan curve, optionally one per TDP preset, guarded by an independent watchdog |
| **Charging estimate** | Shows live battery percentage, net charging power and estimated time to full only while Battery is open |
| **Magic Modules** | Identifies every known module and upper/lower layout, ejects either side and provides Quick Reset |
| **Vibration** | Firmware Off/Low/Medium/High strength, 0-100% Linux FF gain and a vibration test |
| **Lighting** | Solid, Pulse and Rainbow modes with Hue, Saturation and Brightness |
| **Key bindings** | Makes LC, RC, LC1 and RC1 four separate buttons in Steam and supports both QAM firmware variants |
| **TM Guard** | Returns from accidental TM mode changes and restores the saved controller configuration |
| **Smart amp tuning** | Loads the official AYANEO 3 CS35L41 profile for both speakers instead of the generic fallback |
| **Speaker calibration** | Recalibrates both smart amplifiers with validation, EFI readback and automatic backup |
| **OLED definition** | Gamma 2.2 gamescope definition with 60/90/120/144 Hz modes |
| **HDR metadata** | Sets gamescope's EDID copy to AYANEO's advertised 800 nit maximum |
| **Persistent settings** | Restores settings after startup, follows games with the QAM closed and re-applies TDP after charger changes and sleep |
| **Updater** | Shows the installed version and downloads the exact matching GitHub release ZIP from the About page |

---

## Requirements

| Requirement | Details |
|---|---|
| Device | AYANEO 3 OLED, verified on Ryzen 7 8840U and Ryzen AI 9 HX 370 variants |
| OS | SteamOS or Bazzite in Game Mode |
| Plugin loader | [Decky Loader](https://decky.xyz) |
| Privileges | root through Decky's plugin service |

The controller protocol and display identity were verified on retail hardware.
Other AYANEO models are deliberately rejected by the device checks.
The plugin retains its SteamOS 3.8/kernel 6.16 controller and charge-control paths.
TDP control writes SPL, SPPT and FPPT with RyzenAdj and needs no active
PowerStation writer: while PowerStation runs, TDP changes are refused and the
TDP page says why. CPU Boost, EPP and the other controls remain available.
Every write checks ownership again; a cached status never authorizes one.
TDP Control keeps its own settings and serializes its own changes, so its game
tracking never waits behind a controller or audio operation. Fan Control does
the same, so turning it off never waits for a slow settings commit.
Saved charge bypass is verified by a separate monitor every 15 seconds and after
charger changes or resume.
Controller firmware, RGB, bindings and module eject/reset require the vendor HID
interface. If the kernel's `hid-ayaneo` driver owns it, these controls display an
unavailable reason instead of issuing competing commands; evdev FF Gain remains
independent. Module presence and power recovery continue through EC while the
vendor HID interface is temporarily absent after ejecting the modules.
Fan control needs the kernel's `ayaneo-ec` hwmon interface; it is available on
the tested SteamOS 3.10/kernel 7.2.4 system.

> [!NOTE]
> AYANEO 3 Companion is deliberately hardware-specific. It has been tested on
> both AYANEO 3 OLED processor variants, but it does not attempt to control other
> AYANEO handhelds that happen to expose similar interfaces.

---

## Installation

1. Install [Decky Loader](https://decky.xyz) if you haven't already.
2. Download `Ayaneo3Companion-x.x.x.zip` from the [Releases](https://github.com/Rayekkk/Ayaneo3Companion/releases) page.
3. In Gaming Mode, open the **Quick Access Menu**.
4. Open the Decky menu, scroll to the bottom, then **Developer → Install Plugin from ZIP**.
5. Select the downloaded zip.

<details>
<summary><b>Building from source</b></summary>

<br>

The development checks use Node.js 24 and Python 3.10 or 3.13.

```bash
git clone https://github.com/Rayekkk/Ayaneo3Companion
cd Ayaneo3Companion

npm ci --ignore-scripts --no-audit --no-fund
npm run typecheck
npm audit --audit-level=high
npm run test:frontend
npm run test:backend
npm run build
npm run package -- --check
npm run package    # produces Ayaneo3Companion-<version>.zip
```

Then install the resulting zip through Decky's **Install Plugin from ZIP**, which is the
supported path and avoids permission problems.

</details>

### Fresh installation without QAM

On a fresh installation where the QAM button is not available yet, copy
`bootstrap.sh` and the plugin ZIP to the console, then run:

```bash
chmod +x bootstrap.sh
./bootstrap.sh Ayaneo3Companion-1.3.0.zip
```

The bootstrap installer checks the device, installs the plugin directly and
installs its aya7 key-binding extension. Decky Loader must already be installed.
It discovers the plugin directory from the effective Decky systemd service
configuration, including separate privileged and user data directories and
accounts other than `deck`. It validates the complete
ZIP, stages a fresh deployment and replaces the plugin directory as a unit.
Concurrent installers are excluded for the duration of the transaction. The
previous deployment is retained until the restarted services pass their checks.
If a file operation or service restart fails, it restores the previous plugin,
owned InputPlumber maps and service state. If rollback fails, it keeps recovery
files in the reported directory and does not restart Decky after an incomplete
rollback. If Decky cannot be stopped, the installer preserves the running
deployment and recovery files instead of replacing files under the live process.
For a graphical Desktop Mode workflow, keep the ZIP, `bootstrap.sh` and
`Install AYANEO 3 Companion.desktop` in the same folder, mark the launcher as
executable and double-click it.

The display and button sections install persistent system definitions. Restart
Game Mode when the panel asks for it.

---

## Usage

The plugin opens on a compact hardware overview. Each row shows the current
state and opens a dedicated control page; **All Controls** returns to the
overview and selects the row you came from. A page stays open when Quick Access
closes, a dropdown or confirmation opens, or another Quick Access tab is shown,
and polling pauses while the page cannot be seen. The last confirmed overview
state is shown immediately while a fresh read completes.
If a refresh fails, each row says how old its values are and that Companion is
retrying; the note clears after a fresh read.

- Open **TDP** to choose Silent, Balanced, Performance or Custom limits. While a
  game runs, **Settings for** picks Global, **This game** or **This game +
  charger** (separate battery and charger values). Global keeps the game's
  profile unused instead of deleting it. The CPU section sets CPU Boost and the
  CPU energy preference for the same scope; **Show details** shows the package
  power draw.
- Open **Vibration** to select firmware strength, scale Linux force feedback in
  10% steps or play a short test.
- Open **Lighting** to select an animation and tune Hue, Saturation and Brightness.
- Open **Battery** to enable or disable charge bypass. The displayed state is
  read back from the kernel charge-control interface when available, or directly
  from the embedded controller on older kernels; it is never inferred from ACPI.
- Open **Fan Control** to see the APU temperature and fan RPM, and to run your
  own curve: start from Silent, Balanced or Aggressive, move the eight points
  with touch or the D-pad (A to edit, LB/RB to pick a point), and save. Turn it
  off to give the fan back to the console's automatic curve.
- Open **Magic Modules** to identify the installed module type and layout,
  eject either side or both, or run Quick Reset. Insert both modules after
  replacement so the plugin can power and identify the controller again.
- Open **Audio** to inspect or reapply the AYANEO smart-amplifier tuning. Once
  both DSPs are verified, **Recalibrate Audio** becomes available. Put the
  console on a clear surface in a quiet room, press **Start** and confirm the prompt.
  Fully restart the console after a successful calibration so the audio driver
  reads the new EFI values.
- Open **Key Binding** to make LC1/RC1 work next to LC/RC; Steam shows all four under their own names.
  Native F24 and legacy Meta+D QAM events are both recognized automatically.
  TM Guard is enabled by default; disable it temporarily when deliberately
  selecting another hardware mode with the physical TM button.
- Open **OLED display** to install the gamescope definition and verify its HDR metadata.
  The definition lives in your `~/.config/gamescope/scripts`, which SteamOS
  updates keep, so the colours survive an update. gamescope loads definitions
  only when it starts: when the running session has not loaded the current one,
  the page offers **Restart Gaming Mode** (after a confirmation, because Steam
  and any running game close and reopen). If SteamOS ever ships its own AYANEO 3
  profile, Companion leaves the display to it and keeps only the HDR peak fix.
- The Key Binding and OLED switches are saved. If a SteamOS update removes the
  key map from `/etc`, Companion reinstalls it at startup and reloads InputPlumber.
- Open **About** at the bottom of the overview to see the installed version,
  check GitHub releases and download a newer plugin ZIP when one is available.
  The result remains available when returning to About during the same plugin
  session; repeated clicks do not start duplicate downloads.

## How it works

Settings are committed as one complete JSON file using an atomic replacement,
with a last-good backup. A damaged primary file is recovered from that backup;
if neither copy can be read safely, hardware changes stop and the panel explains
the problem. Failed setting changes restore the previous configuration where
the hardware interface supports restoration.

Pending RGB and vibration adjustments are saved even when their control page
closes. Status polls never overlap and stop while QAM is hidden; completed writes and a new
game context cannot be overwritten by an older response. Per-game tracking and
the existing recovery monitors continue independently of the panel.

Custom TDP Apply uses the latest slider choice, including changes
made immediately before pressing Apply; an unapplied Custom draft asks before a
scope change discards it. Speaker discovery and verification run
in the background so other controls can open while ALSA becomes available.
Audio calibration remains unavailable until both speaker DSPs are verified.

GitHub downloads are limited to this plugin's stable release ZIP. The updater
checks the release's SHA-256 digest, archive paths, sizes and plugin identity
before publishing a complete file in Downloads.

TDP Control is LegionGo2Companion's, adapted to the AYANEO 3:

- The ceilings are the ones the console's SMU keeps, measured on a Ryzen 7
  8840U: SPL 30 W and SPPT 43 W; FPPT is limited to 45 W. Performance uses all
  three. The 5 W floor is Companion's.
- RyzenAdj is the only writer: the AYANEO 3 has no firmware TDP interface. The
  pinned, checksum-verified build is kept in `/var/lib/ayaneo3companion-tdp`, so
  a plugin update does not download it again; it is checked before every run.
- On every charger change the firmware lowers SPL to 28 W, so Companion writes
  the limits again after each power-source change and after sleep.
- Stock SteamOS blocks RyzenAdj from reading the limits back on this console
  (the BIOS leaves the SMU's table in memory the kernel marks busy). When the
  kernel is started with `iomem=relaxed`, Companion also shows **Limits now**
  and rewrites limits it finds changed.
- CPU Boost writes Linux CPUFreq's global `boost` control and EPP the CPU
  energy preference of every policy (0–100%, or the kernel's named choices).
  Uninstall puts both back to what they were before Companion first changed them.
- Settings from 1.2.x are carried over once: Low power becomes Silent, Balanced
  and Performance take their new values, and Minimum, Max and Custom stay as
  Custom limits.
- Charge control has its own transaction lock shared with charge recovery, so
  charge changes never wait for other controls.
- Charge bypass prefers the upstream AYANEO `charge_behaviour` kernel ABI. On
  older SteamOS kernels it falls back to the same AYANEO 3 EC register used by
  Linux 6.19's `ayaneo-ec` driver. The plugin verifies every write and restores
  automatic charging before uninstalling.
- Magic Module power and presence use the native `ayaneo-ec` sysfs interface
  where available. On kernel 6.16, the existing raw EC path remains in use;
  an incomplete native interface never triggers a competing raw EC write.
- Fan control writes `pwm1_enable` and `pwm1` of the kernel's `ayaneo-ec` hwmon
  interface and reads every duty back. Fan Control from LegionGo2Companion runs
  the curve in its own root process with a forked backup watchdog: either one
  returns the fan to the firmware's automatic curve when the other dies, the
  plugin unloads or the console prepares to sleep, and the backup sets full
  speed at 100°C on its own. Known fan programs (jupiter-fan-control, HHD,
  fancontrol) block Fan Control instead of competing with it.
- Magic Module identity and upper/lower layout come from AYANEO's vendor HID status,
  with the same known-module table used by HHD. Release uses the vendor eject
  command; after the latch motors finish, the plugin power-cycles the controller
  through the EC and waits for both modules before restoring the saved setup.
  Quick Reset uses AYANEO's module reinitialisation flag and performs the same
  complete restore without releasing either latch.
- Firmware vibration and RGB use the AYANEO vendor HID interface. The FF Gain
  slider writes Linux `FF_GAIN` to the physical gamepad, scaling effects from
  games and the test button without changing the firmware strength.
  An independent evdev monitor restores the latest saved gain after startup,
  resume or physical input-device recreation, including when `hid-ayaneo`
  blocks vendor HID controls. Missing devices and failed writes are retried;
  restoration waits for module operations to finish.
  Device selection requires one of the known internal AYANEO USB physical paths
  for the 8840U and HX 370 variants. A matching external Xbox-compatible pad is
  never used as a fallback while the internal evdev node is absent.
  Combined firmware/RGB and FF Gain edits commit once after both devices succeed;
  failure restores both previous settings before returning an error.
  A controller write or recovery requires a matching custom-mode status report,
  with one additional attempt if verification fails. An ACK alone is insufficient.
  Vibration tests flush pending slider changes and wait for the whole controller
  queue to be confirmed. A failed write prevents the test and refreshes the
  restored settings; FF Gain is disabled while an action is in progress.
- Key bindings extend InputPlumber's native `aya7` capability map instead of
  replacing its APU-specific device profile. The switch programs only the two
  rear-button firmware slots: LC1/RC1 emit KeyL/KeyR (InputPlumber `LeftTop`/
  `RightTop`), while LC/RC's native F21/F22 become `LeftPaddle2`/`RightPaddle2`;
  Steam's controller tester shows them as LC1, RC1, LC and RC. TM mode wipes these slots, so TM Guard
  programs them again whenever it leaves TM mode. Both F24 and legacy Meta+D are accepted as QAM;
  the inactive firmware variant never emits its mapping.
- TM Guard polls AYANEO's controller-mode status. When TM leaves custom gamepad
  mode, it cuts the controller's power through the EC for two seconds, then sends
  the custom-mode command, resets the gamepad's USB port (it otherwise keeps its
  TM identity) and reapplies RGB, vibration, FF Gain and the button table.
  Measured on the console: switching back without the power cycle could leave a
  D-pad direction from TM mode latched as a held arrow key that no other reset
  clears. A D-pad still held during the switch can latch one too; TM Guard
  notices the arrow on the controller's own keyboard (which never sends arrows
  in custom mode) and power-cycles again. It does not remap the TM button.
- The audio fix creates the two side-specific coefficient aliases expected by
  Linux for `speaker_id=1`, using the AYANEO firmware already installed by
  SteamOS. It briefly releases PipeWire's physical PCM and reloads only the
  left and right DSP firmware controls.
- Recalibration loads the CS35L41 calibration profile, measures both channels,
  rejects implausible resistance changes and writes the existing Cirrus EFI
  format without changing either amplifier ID. The original variable is saved
  under `/home/.ayaneo3companion-backups/audio-calibration` before every write
  (`/home` survives SteamOS slot updates; backups older releases kept in
  `/var/lib/ayaneo3-companion` are copied there at startup). A full restart is
  required before the driver applies the new values.
- RyzenAdj and calibration-firmware downloads use incremental reads, byte limits
  and a 60-second transfer deadline checked between network reads. Individual
  network operations have a 10-second timeout; a pending read may finish after
  the transfer deadline. Interrupted binary downloads retain existing components.
- The display definition matches `AYA / 0x0113 / AYAOLED_FHD`. Root writes it into
  the gamescope user's folder only through a helper running as that user, so a
  link planted there cannot redirect the write. A runtime check
  normalizes only the CTA MaxCLL byte to AYANEO's advertised 800 nit maximum if
  the bridge or another tool publishes a different value.

---

## Development

The complete verification sequence is:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm audit --audit-level=high
npm run typecheck
npm run test:frontend
npm run test:backend
npm run build
npm run package -- --check
npm run package
```

Backend tests run without AYANEO hardware, using isolated temporary files and
mocked device interfaces. The frontend suite runs the actual components with
controlled RPC completion order to exercise saves, polling, game changes,
dropdowns and panel lifecycles. GitHub Actions runs the backend suite on Ubuntu
with Python 3.10 and Windows with Python 3.13, then checks the frontend and the
contents of the built ZIP with Node.js 24.
Settings writes return their confirmed state without waiting for unrelated
diagnostics; the UI merges only the action's own fields into the latest observed
state. TDP edits carry the game they were made for, and the backend refuses
them once another game runs. Regular status reads refresh capabilities.
Responsiveness tests block a slow read while subsequent controller transactions
complete. TDP Control's own tests, ported from LegionGo2Companion, run its page
against delayed replies and game changes and its backend against a temporary
CPUFreq tree, a fake RyzenAdj and the SMU's measured clamps. Fan Control's
guardian, also from the Legion, runs as real processes on a model hwmon
directory and is killed, frozen and suspended mid-write. Additional
regressions cover HID confirmation and bounded hardware transfers. They also
hold a TDP transaction while charge and RGB changes finish, verify charge recovery
reads committed intent, and exercise vibration tests during queued saves.
Installer regressions exercise real directory swaps, failed staging, map writes,
service failures and incomplete rollback using temporary files and fake services.
The archive check also uses the updater's runtime-file validator. The build uses
direct Rollup plugins and native filesystem cleanup without the obsolete
glob/delete dependency chain.

These checks do not verify live hardware. Startup, suspend/resume, charger
changes, module reconnection and physical controller behaviour still require
validation on an AYANEO 3 before claiming a hardware-tested release.

---

## License

BSD 3-Clause. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
