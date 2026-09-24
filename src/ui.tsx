// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { ButtonItem, Field, gamepadDialogClasses, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, ReactNode } from "react";

export const PageShell: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" }}>
    {children}
  </div>
);

const Chevron: FC = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    style={{ display: "block", width: "1em", height: "1em", flexShrink: 0 }}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

export const SectionLink: FC<{
  title: string;
  description: string;
  statusNote?: string;
  onClick: () => void;
}> = ({ title, description, statusNote, onClick }) => (
  <PanelSectionRow>
    <Field
      label={title}
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

export const SectionHeader: FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => (
  <PanelSection title={title}>
    <PanelSectionRow>
      <ButtonItem layout="below" onClick={onBack}>‹ All Controls</ButtonItem>
    </PanelSectionRow>
  </PanelSection>
);

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
