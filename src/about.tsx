// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC } from "react";
import { UpdateSection } from "./updates";

export const AboutPage: FC<{ version: string }> = ({ version }) => <>
  <PanelSection title="AYANEO 3 Companion">
    <PanelSectionRow>
      <Field focusable label={`Version ${version}`} description="All-in-one AYANEO 3 hardware controls for Decky Loader." />
    </PanelSectionRow>
    <PanelSectionRow>
      <Field focusable label="Included modules" description="TDP & CPU · Vibration · RGB Lighting · Battery · Magic Modules · Audio · Key Binding · OLED Display · Fan Control" />
    </PanelSectionRow>
    <PanelSectionRow>
      <Field focusable label="Author" description="Rayek · BSD-3-Clause open-source plugin." />
    </PanelSectionRow>
  </PanelSection>
  <UpdateSection initialVersion={version} />
</>;
