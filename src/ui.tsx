// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// Page shell, overview rows and page header from LegionGo2Companion.

import { useQuickAccessVisible } from "@decky/api";
import { ButtonItem, Field, gamepadDialogClasses, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, ReactNode, useEffect, useRef } from "react";

export type SectionKey = "tdp" | "vibration" | "rgb" | "fan" | "battery" | "modules" | "audio" | "buttons" | "screen" | "about";

export const PageShell: FC<{ children: ReactNode; rootRef?: { current: HTMLDivElement | null } }> = ({ children, rootRef }) => (
  <div ref={rootRef} data-companion-page style={{ width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" }}>
    {children}
  </div>
);

const Chevron: FC = () => (
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    style={{ display: "block", width: "1em", height: "1em", flexShrink: 0 }}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

// Field supplies Steam's label, spacing, focus highlight and activation. The
// decorative icons inherit that same color, including its focused appearance.
const sectionIconPaths: Record<SectionKey, ReactNode> = {
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
const SectionIcon: FC<{ section: SectionKey }> = ({ section }) => (
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"
    style={{ display: "block", width: "1.2em", height: "1.2em", flexShrink: 0 }}>
    {sectionIconPaths[section]}
  </svg>
);

export const SectionLink: FC<{
  section: SectionKey;
  title: string;
  description: string;
  statusNote?: string;
  restoreFocus?: boolean;
  onClick: () => void;
}> = ({ section, title, description, statusNote, restoreFocus, onClick }) => {
  const fieldRef = useRef<HTMLDivElement>(null);
  const visible = useQuickAccessVisible();
  useEffect(() => {
    if (restoreFocus && visible) fieldRef.current?.focus();
    // Returning to the list restores its selection once. QAM/dropdown visibility
    // and status polling must not move the user's current Steam focus.
  }, [restoreFocus]);
  return (
    <PanelSectionRow>
      <Field
        ref={fieldRef}
        label={title}
        icon={<SectionIcon section={section} />}
        bottomSeparator="standard"
        verticalAlignment="center"
        description={statusNote ? <>{description}<div style={{ fontSize: "11px", opacity: 0.7, marginTop: "2px" }}>{statusNote}</div></> : description}
        childrenLayout="inline"
        childrenContainerWidth="min"
        focusable
        highlightOnFocus
        onActivate={onClick}
      >
        <Chevron />
      </Field>
    </PanelSectionRow>
  );
};

export const SectionHeader: FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => {
  const backRef = useRef<HTMLDivElement>(null);
  const visible = useQuickAccessVisible();
  useEffect(() => {
    if (visible) backRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    // One focus handoff on page entry, never on a poll or dropdown dismissal.
  }, []);
  return <PanelSection title={title}>
    <PanelSectionRow>
      <div ref={backRef}><ButtonItem layout="below" onClick={onBack}>‹ All Controls</ButtonItem></div>
    </PanelSectionRow>
  </PanelSection>;
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
