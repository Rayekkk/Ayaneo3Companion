// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The overview's sections (keys, titles, icons) and the stacked action row of the device pages.

import { ButtonItem, gamepadDialogClasses, PanelSectionRow } from "@decky/ui";
import { FC, ReactNode } from "react";

export type SectionKey = "tdp" | "vibration" | "rgb" | "fan" | "battery" | "modules" | "audio" | "buttons" | "screen" | "about";

export const SECTION_TITLES: Record<SectionKey, string> = {
  tdp: "TDP", fan: "Fan Control", vibration: "Vibration", rgb: "Lighting", buttons: "Key binding",
  modules: "Magic Modules", battery: "Battery", audio: "Audio", screen: "OLED display", about: "About",
};

// Paths only: ui.tsx's Icon draws them in Steam's text and focus color.
export const sectionIcons: Record<SectionKey, ReactNode> = {
  tdp: <path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z" />,
  fan: <><circle cx="12" cy="12" r="2" /><path d="M12 10C4 1 1 12 10 12M14 12c9-8-2-11-2-2M12 14c8 9 11-2 2-2M10 12c-9 8 2 11 2 2" /></>,
  vibration: <><rect x="7" y="5" width="10" height="14" rx="2" /><path d="m3 7-1 3 1 4-1 3m19-10 1 3-1 4 1 3" /></>,
  rgb: <><path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-3.7 2 2 0 0 1 1-3.3h2a4 4 0 0 0 4-4c0-4-4-7-9-7Z" /><circle cx="7" cy="10" r=".8" /><circle cx="10" cy="7" r=".8" /><circle cx="15" cy="7" r=".8" /></>,
  buttons: <><path d="M7 7h10a4 4 0 0 1 4 4l-1 7-5-3H9l-5 3-1-7a4 4 0 0 1 4-4Z" /><path d="M6 11h4m-2-2v4m7-2h.01m3 2h.01" /></>,
  modules: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  battery: <><rect x="3" y="6" width="17" height="12" rx="2" /><path d="M22 10v4m-9-6-4 5h5l-3 4" /></>,
  audio: <><path d="M4 9h4l5-4v14l-5-4H4Z" /><path d="M17 9a4 4 0 0 1 0 6m2-9a8 8 0 0 1 0 12" /></>,
  screen: <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8m-4-4v4" /></>,
  about: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10h.01" /></>,
};

export const StackedAction: FC<{
  title: string; description: string; disabled?: boolean; onClick: () => void; children: ReactNode;
}> = ({ title, description, disabled, onClick, children }) => (
  <PanelSectionRow>
    <div style={{ width: "100%", maxWidth: "100%", minWidth: 0, boxSizing: "border-box" }}>
      <div className={gamepadDialogClasses.FieldLabel} style={{ marginBottom: "6px" }}>{title}</div>
      <div className={gamepadDialogClasses.FieldDescription} style={{ marginBottom: "8px", overflowWrap: "anywhere" }}>{description}</div>
      <div style={{ width: "100%", maxWidth: "100%", minWidth: 0 }}>
        <ButtonItem layout="below" bottomSeparator="none" disabled={disabled} onClick={onClick}>{children}</ButtonItem>
      </div>
    </div>
  </PanelSectionRow>
);
