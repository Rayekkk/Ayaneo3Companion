// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable } from "@decky/api";
import { Field, PanelSection, PanelSectionRow, SliderField, Spinner } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { StackedAction } from "./ui";

type FanMode = "auto" | "manual";
export interface FanStatus {
  supported: boolean;
  mode: FanMode;
  percent: number | null;
  rpm: number | null;
  reason?: string;
}

const getFanStatus = callable<[], FanStatus>("get_fan_status");
const setFan = callable<[FanMode, number?], FanStatus>("set_fan");
let confirmedFanStatus: FanStatus | null = null;
// Only a successful hardware read establishes capability. A later read failure
// must not remove the user's route back to Auto, or invent support on old kernels.
let fanDetected = false;

export const resetFanStatus = () => { confirmedFanStatus = null; fanDetected = false; };

export function useFanControl(visible: boolean, pageOpen: boolean) {
  const [status, setStatus] = useState<FanStatus | null>(confirmedFanStatus);
  const [available, setAvailable] = useState(fanDetected);
  const [readError, setReadError] = useState(confirmedFanStatus && !confirmedFanStatus.supported
    ? confirmedFanStatus.reason || "Could not read the fan controller." : "");
  const [actionError, setActionError] = useState("");
  const error = actionError || readError;
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const visibleRef = useRef(visible);
  const reading = useRef<Promise<FanStatus> | null>(null);
  const writing = useRef(false);
  const revision = useRef(0);
  visibleRef.current = visible;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; ++revision.current; };
  }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !visibleRef.current || writing.current || reading.current) return;
    const request = getFanStatus();
    const ticket = revision.current;
    reading.current = request;
    try {
      const next = await request;
      if (!mounted.current || !visibleRef.current || ticket !== revision.current) return;
      confirmedFanStatus = next;
      if (next.supported) { fanDetected = true; setAvailable(true); }
      setStatus(next);
      setReadError(next.supported ? "" : next.reason || "Could not read the fan controller.");
    } catch (failure) {
      if (mounted.current && visibleRef.current && ticket === revision.current)
        setReadError(failure instanceof Error ? failure.message : "Could not read the fan controller.");
    } finally {
      if (reading.current === request) reading.current = null;
    }
  }, []);

  useEffect(() => {
    ++revision.current;
    if (!visible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), pageOpen ? 5000 : 30000);
    return () => { clearInterval(timer); ++revision.current; };
  }, [visible, pageOpen, refresh]);

  const apply = async (mode: FanMode, percent?: number) => {
    if (writing.current || !available || (mode === "manual" && (!status?.supported || error))) return;
    writing.current = true;
    const ticket = ++revision.current;
    setBusy(true); setActionError("");
    try {
      const next = await setFan(mode, ...(mode === "manual" ? [percent] : []));
      if (!mounted.current || ticket !== revision.current) return;
      confirmedFanStatus = next;
      if (next.supported) { fanDetected = true; setAvailable(true); }
      setStatus(next);
      setReadError(next.supported ? "" : next.reason || "Could not read the fan controller.");
      if (!next.supported) setActionError(next.reason || "The fan change could not be confirmed.");
    } catch (failure) {
      if (mounted.current && ticket === revision.current)
        setActionError(failure instanceof Error ? failure.message : "The fan change could not be confirmed.");
    } finally {
      writing.current = false;
      if (mounted.current) { setBusy(false); if (visibleRef.current) void refresh(); }
    }
  };

  return { status, available, error, busy, apply };
}

export const FanPage: FC<{
  status: FanStatus | null;
  available: boolean;
  error: string;
  busy: boolean;
  onApply(mode: FanMode, percent?: number): void;
}> = ({ status, available, error, busy, onApply }) => {
  const [draft, setDraft] = useState(100);
  useEffect(() => {
    if (status?.mode === "manual" && status.percent != null)
      setDraft(Math.max(60, Math.min(100, Math.round(status.percent))));
  }, [status?.mode, status?.percent]);

  return <>
    <PanelSection title="Fan Status">
      {!status
        ? <PanelSectionRow>{error ? <Field focusable label="Fan status unavailable" description={error} /> : <Spinner />}</PanelSectionRow>
        : <PanelSectionRow><Field focusable
            label={status.supported ? status.mode === "manual" ? "Manual control" : "Automatic control" : "Fan control unavailable"}
            description={status.supported
              ? `${status.rpm == null ? "RPM unavailable" : `${status.rpm} RPM`}${status.mode === "manual" && status.percent != null ? ` · ${status.percent}% requested` : ""}`
              : status.reason || "The AYANEO EC fan interface is not available on this system."}
          /></PanelSectionRow>}
      {error && status && <PanelSectionRow><Field focusable label="Could not confirm fan state" description={error} /></PanelSectionRow>}
    </PanelSection>
    {available && <PanelSection title="Fan Control">
      <PanelSectionRow><Field focusable label="Automatic mode" description="The console adjusts fan speed itself. Manual control returns to Auto when Companion stops or its safety timer expires." /></PanelSectionRow>
      <StackedAction title="Restore Automatic Control" description="Use the console's built-in fan curve." disabled={busy || Boolean(status?.supported && status.mode === "auto" && !error)} onClick={() => onApply("auto")}>{busy ? <Spinner /> : "Auto"}</StackedAction>
      <PanelSectionRow><SliderField label="Manual speed" description="Choose 60–100%. Apply to change the fan; moving the slider alone does not affect cooling." value={draft} min={60} max={100} step={5} notchCount={9} notchTicksVisible validValues="steps" minimumDpadGranularity={5} valueSuffix="%" onChange={setDraft} disabled={busy || !status?.supported || Boolean(error)} /></PanelSectionRow>
      <StackedAction title={status?.mode === "manual" ? "Apply Manual Speed" : "Enable Manual Control"} description="Companion checks the result and restores Auto if manual control is interrupted." disabled={busy || !status?.supported || Boolean(error) || (status.mode === "manual" && status.percent === draft)} onClick={() => onApply("manual", draft)}>{busy ? <Spinner /> : `Apply ${draft}%`}</StackedAction>
    </PanelSection>}
  </>;
};
