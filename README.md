<div align="center">

# AYANEO 3 Companion

[![Build](https://img.shields.io/github/actions/workflow/status/Rayekkk/Ayaneo3Companion/build.yml?branch=main&style=for-the-badge&label=build&labelColor=141417)](https://github.com/Rayekkk/Ayaneo3Companion/actions/workflows/build.yml)
[![Device](https://img.shields.io/badge/device-AYANEO_3_OLED-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/github/license/Rayekkk/Ayaneo3Companion?style=for-the-badge&label=license&color=424A53&labelColor=141417)](LICENSE)

**The missing AYANEO 3 hardware controls in one Steam overlay panel.**
TDP profiles, smart-amp audio, battery bypass, Magic Modules, vibration, RGB lighting, rear buttons and OLED display support.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [How it works](#how-it-works) · [Building](#building)

</div>

---

## Features

| | |
|---|---|
| **TDP presets** | Minimum, Low Power, Balanced, Performance and Max profiles tuned for the AYANEO 3 |
| **Custom TDP** | SPL up to 35 W, with SPPT and FPPT capped at 37 W |
| **Per-game profiles** | Saves a TDP profile for the running game and applies it automatically |
| **Live power** | Shows current package power in the TDP panel |
| **Charge bypass** | Powers the console from its charger while inhibiting battery charging |
| **Charging estimate** | Shows live battery percentage, net charging power and estimated time to full only while Battery is open |
| **Magic Modules** | Identifies every known module and upper/lower layout, ejects either side and provides Quick Reset |
| **Vibration** | Firmware Off/Low/Medium/High strength, 0-100% Linux FF gain and a vibration test |
| **RGB lighting** | Solid, Pulse and Rainbow modes with Hue, Saturation and Brightness |
| **Key bindings** | Programs LC1/RC1 as L5/R5, preserves LC/RC as L4/R4 and supports both QAM firmware variants |
| **TM Guard** | Returns from accidental TM mode changes and restores the saved controller configuration |
| **Smart amp tuning** | Loads the official AYANEO 3 CS35L41 profile for both speakers instead of the generic fallback |
| **Speaker calibration** | Recalibrates both smart amplifiers with validation, EFI readback and automatic backup |
| **OLED definition** | Gamma 2.2 gamescope definition with 60/90/120/144 Hz modes |
| **HDR metadata** | Corrects the gamescope EDID copy so games receive the panel's 800 nit peak |
| **Persistent settings** | Restores settings after startup, follows games with the QAM closed and re-applies TDP after charger changes |

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

---

## Installation

No release archive is published yet. Build the plugin from source, then install
the generated zip through **Decky → Settings → Developer → Install Plugin from ZIP**.

On a fresh installation where the QAM button is not available yet, copy
`bootstrap.sh` and the plugin ZIP to the console, then run:

```bash
chmod +x bootstrap.sh
./bootstrap.sh Ayaneo3Companion-0.7.0.zip
```

The bootstrap installer checks the device, installs the plugin directly and
installs its aya7 key-binding extension. Decky Loader must already be installed.
For a graphical Desktop Mode workflow, keep the ZIP, `bootstrap.sh` and
`Install AYANEO 3 Companion.desktop` in the same folder, mark the launcher as
executable and double-click it.

The display and button sections install persistent system definitions. Restart
Game Mode when the panel asks for it.

## Usage

The plugin opens on a compact hardware overview. Each row shows the current
state and opens a dedicated control page; **All Controls** returns to the
overview. Closing and reopening QAM always returns to this overview.

- Open **TDP** to select a preset or create a Custom configuration. Enable
  **Per Game Profile** while a game is running to bind the chosen values to it.
- Open **Vibration** to select firmware strength, scale Linux force feedback in
  10% steps or play a short test.
- Open **RGB** to select an animation and tune Hue, Saturation and Brightness.
- Open **Battery** to enable or disable charge bypass. The displayed state is
  read back from the embedded controller rather than inferred from ACPI.
- Open **Magic Modules** to identify the installed module type and layout,
  eject either side or both, or run Quick Reset. Insert both modules after
  replacement so the plugin can power and identify the controller again.
- Open **Audio** to inspect or reapply the AYANEO smart-amplifier tuning. Once
  both DSPs are verified, **Recalibrate Audio** becomes available. Put the
  console on a clear surface and confirm the second prompt before calibration.
  Fully restart the console after a successful calibration so the audio driver
  reads the new EFI values.
- Open **Key Binding** to preserve LC/RC as L4/R4 and add LC1/RC1 as L5/R5.
  Native F24 and legacy Meta+D QAM events are both recognized automatically.
  TM Guard is enabled by default; disable it temporarily when deliberately
  selecting another hardware mode with the physical TM button.
- Open **OLED display** to install the gamescope definition and monitor the EDID correction.

## How it works

The Minimum preset uses 5/8/10 W. The Max preset uses 32 W SPL, 35 W SPPT and
37 W FPPT; Custom tuning is also capped at 37 W.

- On Bazzite, TDP is written through `org.shadowblip.PowerStation`. On SteamOS,
  the plugin downloads the pinned RyzenAdj build and writes SPL/SPPT/FPPT directly.
- Charge bypass uses the AYANEO 3 EC charge-control register exposed through
  the kernel's signed `ec_sys` module. The plugin verifies every write and
  restores automatic charging before uninstalling.
- Magic Module identity and upper/lower layout come from AYANEO's vendor HID status,
  with the same known-module table used by HHD. Release uses the vendor eject
  command; after the latch motors finish, the plugin power-cycles the controller
  through the EC and waits for both modules before restoring the saved setup.
  Quick Reset uses AYANEO's module reinitialisation flag and performs the same
  complete restore without releasing either latch.
- Firmware vibration and RGB use the AYANEO vendor HID interface. The FF Gain
  slider writes Linux `FF_GAIN` to the physical gamepad, scaling effects from
  games and the test button without changing the firmware strength.
- Key bindings extend InputPlumber's native `aya7` capability map instead of
  replacing its APU-specific device profile. The switch programs only the two
  rear-button firmware slots: LC1/RC1 emit KeyL/KeyR and become L5/R5, while
  native F21/F22 remain L4/R4. Both F24 and legacy Meta+D are accepted as QAM;
  the inactive firmware variant never emits its mapping.
- TM Guard polls only AYANEO's controller-mode status. If TM changes away from
  custom gamepad mode, it sends the official custom-mode command and reapplies
  RGB, vibration, FF Gain and enabled rear-button mappings. It does not consume
  or remap the physical TM button itself.
- The audio fix creates the two side-specific coefficient aliases expected by
  Linux for `speaker_id=1`, using the AYANEO firmware already installed by
  SteamOS. It briefly releases PipeWire's physical PCM and reloads only the
  left and right DSP firmware controls.
- Recalibration loads the CS35L41 calibration profile, measures both channels,
  rejects implausible resistance changes and writes the existing Cirrus EFI
  format without changing either amplifier ID. The original variable is saved
  under `/var/lib/ayaneo3-companion/audio-calibration` before every write. A
  full restart is required before the driver applies the new values.
- The display definition matches `AYA / 0x0113 / AYAOLED_FHD`. A runtime
  correction updates only the CTA MaxCLL byte in gamescope's EDID copy.

## Building

Requires Node.js 18+ and Python 3.10+.

```bash
git clone https://github.com/Rayekkk/Ayaneo3Companion
cd Ayaneo3Companion
npm ci
npm run typecheck
npm run build
python -m unittest discover -s tests -v
npm run package
```

## License

BSD-3-Clause. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
