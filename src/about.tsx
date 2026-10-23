// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { PanelSection } from "@decky/ui";
import { FC } from "react";
import { infoRow } from "./shared";
import { UpdateSection } from "./updates";

export const AboutPage: FC<{ version: string }> = ({ version }) => <>
  <PanelSection title="AYANEO 3 Companion">
    {infoRow(`Version ${version}`, "All-in-one AYANEO 3 hardware controls for Decky Loader.")}
    {infoRow("Included modules", "TDP & CPU · Fan Control · Vibration · Lighting · Key Binding · Magic Modules · Battery · Audio · OLED Display")}
    {infoRow("Author", "Rayek · BSD-3-Clause open-source plugin.")}
  </PanelSection>
  <UpdateSection initialVersion={version} />
</>;
