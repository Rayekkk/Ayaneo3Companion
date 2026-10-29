// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// Colour conversion and the slider of the Lighting page (index.tsx).

import { SliderField } from "@decky/ui";
import { FC } from "react";

export function hsvToHex(hue: number, saturation: number): string {
  const h = ((hue % 360) + 360) % 360;
  const s = clamp(saturation, 0, 100) / 100;
  const c = s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = 1 - c;
  const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
    : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
  return [r, g, b].map(channel => Math.round((channel + m) * 255).toString(16).padStart(2, "0")).join("");
}

export function hexToHsv(hex: string): { hue: number; saturation: number } {
  const value = hex.replace("#", "").padEnd(6, "0").slice(0, 6);
  const [r, g, b] = [0, 2, 4].map(offset => parseInt(value.slice(offset, offset + 2), 16) / 255);
  if (![r, g, b].every(Number.isFinite)) return { hue: 0, saturation: 100 };
  const max = Math.max(r, g, b), min = Math.min(r, g, b), delta = max - min;
  if (delta === 0) return { hue: 0, saturation: 0 };
  const hue = max === r ? 60 * (((g - b) / delta) % 6)
    : max === g ? 60 * ((b - r) / delta + 2) : 60 * ((r - g) / delta + 4);
  return { hue: Math.round((hue + 360) % 360), saturation: Math.round(delta / max * 100) };
}

interface SlowSliderProps {
  label: string; value: number; min: number; max: number; className: string; valueSuffix: string;
  disabled?: boolean;
  onChange(value: number): void; onChangeEnd(value: number): void;
}

// The parent owns the debounce and write queue, so leaving the RGB page cannot
// discard a pending slider value or send an older sibling slider closure.
export const SlowSliderField: FC<SlowSliderProps> = slider => <SliderField
  label={slider.label} disabled={slider.disabled} value={slider.value} min={slider.min} max={slider.max}
  validValues="range" showValue valueSuffix={slider.valueSuffix} className={slider.className}
  onChange={value => { if (!slider.disabled) { slider.onChange(value); slider.onChangeEnd(value); } }}
/>;

export const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
