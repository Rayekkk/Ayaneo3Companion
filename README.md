<div align="center">

# AYANEO 3 Companion

[![Build](https://img.shields.io/github/actions/workflow/status/Rayekkk/Ayaneo3Companion/build.yml?branch=main&style=for-the-badge&label=build&labelColor=141417)](https://github.com/Rayekkk/Ayaneo3Companion/actions/workflows/build.yml)
[![Device](https://img.shields.io/badge/device-AYANEO_3_OLED-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/github/license/Rayekkk/Ayaneo3Companion?style=for-the-badge&label=license&color=424A53&labelColor=141417)](LICENSE)

**The missing AYANEO 3 hardware controls in one Steam overlay panel.**
TDP profiles, vibration, RGB lighting, rear buttons and OLED display support.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [How it works](#how-it-works) · [Building](#building)

</div>

---

## Features

| | |
|---|---|
| **TDP presets** | Low Power, Balanced and Performance profiles tuned for the AYANEO 3 |
| **Custom TDP** | SPL, SPPT and FPPT controls up to the official 35 W ceiling |
| **Per-game profiles** | Saves a TDP profile for the running game and applies it automatically |
| **Live power** | Shows current package power in the TDP panel |
| **Vibration** | Off, Low, Medium and High strength plus a 0.5 second test |
| **RGB lighting** | Solid, Pulse and Rainbow modes with Hue, Saturation and Brightness |
| **Menu and rear buttons** | Correct Steam/QAM mapping plus LC1 as L4 and RC1 as R4 |
| **OLED definition** | Gamma 2.2 gamescope definition with 60/90/120/144 Hz modes |
| **HDR metadata** | Corrects the gamescope EDID copy so games receive the panel's 800 nit peak |
| **Persistent settings** | Restores hardware settings after startup and follows games with the QAM closed |

---

## Requirements

| Requirement | Details |
|---|---|
| Device | AYANEO 3 OLED, verified on Ryzen 7 8840U / 16 GB |
| OS | SteamOS or Bazzite in Game Mode |
| Plugin loader | [Decky Loader](https://decky.xyz) |
| Privileges | root through Decky's plugin service |

The controller protocol and display identity were verified on retail hardware.
Other AYANEO models are deliberately rejected by the device checks.

---

## Installation

No release archive is published yet. Build the plugin from source, then install
the generated zip through **Decky → Settings → Developer → Install Plugin from ZIP**.

The display and button sections install persistent system definitions. Restart
Game Mode when the panel asks for it.

## Usage

All sections start collapsed whenever the plugin opens.

- Open **TDP** to select a preset or create a Custom configuration. Enable
  **Per Game Profile** while a game is running to bind the chosen values to it.
- Open **Vibration** to select a strength or play a short test.
- Open **RGB** to select an animation and tune Hue, Saturation and Brightness.
- Open **Menu buttons** to install the persistent InputPlumber mapping.
- Open **OLED display** to install the gamescope definition and monitor the EDID correction.

## How it works

- On Bazzite, TDP is written through `org.shadowblip.PowerStation`. On SteamOS,
  the plugin downloads the pinned RyzenAdj build and writes SPL/SPPT/FPPT directly.
- Vibration and RGB use the AYANEO vendor HID interface. The test button sends
  a standard Linux force-feedback effect without changing the saved firmware setting.
- Button mappings are installed as a device-specific InputPlumber override.
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
