// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The Notice, Power, Custom limits and More sections of the TDP page (tdp.tsx).

import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { unwrap } from "./shared";
import { confirmAction, detailsRows, infoRow } from "./ui";
import {
  absolute, boostRoomNote, drawText, fromAbsolute, limitsText, normalise, offsetMax, powerSummary, PRESET_LABELS,
  PRESET_ORDER, presetNote, resolvedPreset, scopeNote, simpleTuning, sourceNote, tdpNotice, withFppt, withSpl, withSppt,
  type PresetKey, type Tuning,
} from "./tdp_model";
import {
  applyTdp, deleteGameProfile, getGameProfile, getSettings, retryHelper, savedTuning, setAdvancedTdpControlCall,
  setGameAcProfile, setGameProfileActive, setPluginEnabled, toMw, toW, type GameProfile,
} from "./tdp_api";
import type { TdpState } from "./tdp_state";


type Scope = "global" | "game" | "game_ac";

// Sections are plain render functions without hooks, called inline by TdpPage:
// the page keeps one element tree and one hook order.

export const noticeSection = (s: TdpState) => {
  const { capsNow, profileError, profilePending, setProfilePending, setProfileError,
    setProfileAttempt } = s;
  const notice = tdpNotice(capsNow);
  return (notice || profileError) && <PanelSection>
    {notice && infoRow(notice[0], notice[1])}
    {profileError && <>
      {infoRow("Could not load profile", profileError)}
      <PanelSectionRow><ButtonItem layout="below" onClick={() => {
        if (profilePending) return;
        setProfilePending(true); setProfileError("");
        setProfileAttempt(attempt => attempt + 1);
      }}>Retry profile</ButtonItem></PanelSectionRow>
    </>}
  </PanelSection>;
};

export const powerSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirtyRef,
    draftBaseRef, preset, setPreset, tuning, setTuning, acTuning, setAcTuning, active, setActive,
    advancedTdpControl, caps, minW, presets, editingAc, setEditingAc, perGame, setPerGame, game,
    acSeparate, setAcSeparate, acOnline, tdpAvailable, enabled, setEnabled, setSavedPreset, savedPreset,
    setSavedAcPreset, savedAcPreset, globalProfile, setGlobalProfile, adoptGlobalSettings, showGameProfile,
    setProfileAttempt, profilePending, profileError, info, cpuBusy, loading,
    cpuBusyOwnersRef, running: backendRunning } = s;
  /** Run `proceed` now, or after confirming that the unsaved Custom limits are dropped. */
  const askDiscard = (proceed: () => void) => {
    if (!dirtyRef.current) return proceed();
    confirmAction("Discard unsaved changes?", "Your changes to these limits haven't been applied.", "Discard", () => {
      const base = draftBaseRef.current;
      if (base) (base.ac ? setAcTuning : setTuning)(base.tuning);
      clearDraft();
      proceed();
    }, { destructive: true });
  };

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const scope = beginOperation();
    if (!scope) return;
    const prevPreset = preset;
    const prevTuning = tuning, prevAcTuning = acTuning;
    // A preset replaces the Custom sliders, so their draft is dropped without asking.
    clearDraft();
    setPreset(key);
    if (key === "custom") {
      if (!advancedTdpControl) setActive(simpleTuning(active.spl, caps, minW));
      finishOperation(scope);
      return;
    }

    const vals = presets[key];
    const next = normalise(fromAbsolute(vals.spl, vals.sppt, vals.fppt), caps, minW);
    if (editingAc) setAcTuning(next); else setTuning(next);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(next);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, key);
        if (!operationCurrent(scope)) return;
        if (!r.ok) throw new Error(r.error || "The preset was not saved.");
        setSavedAcPreset(key);
        done(`${PRESET_LABELS[key]} saved for ${game!.name} on charger.`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, key,
          scope.appId);
        if (!operationCurrent(scope)) return;
        if (!r.ok) throw new Error(r.error || "The preset was not applied.");
        if (!appId) setGlobalProfile({ ...a, preset: key });
        else setSavedPreset(key);
        done(appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`);
      }
    } catch (e: unknown) {
      if (!operationCurrent(scope)) return;
      setPreset(prevPreset);
      if (editingAc) setAcTuning(prevAcTuning); else setTuning(prevTuning);
      showError("preset", e);
    } finally { finishOperation(scope); }
  };
  // ── Settings for {game} ───────────────────────────────────────────────────────
  const scope: Scope = !perGame ? "global" : acSeparate ? "game_ac" : "game";
  const applyScope = async (next: Scope) => {
    if (!game) return;
    const op = beginOperation(tdpAvailable);
    if (!op) return;
    const app = game;
    try {
      // Without a TDP provider the choice only selects which CPU settings to edit.
      if (!tdpAvailable) { setPerGame(next !== "global"); return; }
      if (next === "global") {
        const result = await setGameProfileActive(app.appId, false);
        if (!operationCurrent(op)) return;
        if (!result.ok) throw new Error(result.error || "Could not switch to Global settings.");
        setPerGame(false); setAcSeparate(false); setEditingAc(false);
        setSavedPreset(undefined); setSavedAcPreset(undefined);
        const s = await unwrap(getSettings());
        if (!operationCurrent(op)) return;
        adoptGlobalSettings(s);
        done(`${app.name} uses Global settings.`);
        return;
      }
      let profile: GameProfile | null = null;
      if (!perGame) {
        const result = await setGameProfileActive(app.appId, true);
        if (!operationCurrent(op)) return;
        if (!result.ok) throw new Error(result.error || "Could not turn on this game's settings.");
        profile = await unwrap(getGameProfile(app.appId));
        if (!operationCurrent(op)) return;
        if (!profile.exists || !profile.profile) throw new Error("Game profile data is missing or corrupt.");
        showGameProfile(profile);
      }
      const separate = profile ? profile.ac_separate : acSeparate;
      if ((next === "game_ac") !== separate) {
        // Hand the stored charger values back unchanged, so splitting again restores them.
        const stored = profile ?? await unwrap(getGameProfile(app.appId));
        if (!operationCurrent(op)) return;
        const ac = stored.ac_profile;
        if (!stored.exists || !ac) throw new Error("Game profile data is missing or corrupt.");
        const result = await setGameAcProfile(app.appId, ac.spl, ac.sppt, ac.fppt, next === "game_ac", "");
        if (!operationCurrent(op)) return;
        if (!result.ok) throw new Error(result.error || "Could not change the charger settings.");
        setAcSeparate(next === "game_ac");
        setAcTuning(savedTuning(ac, ac.ac_preset, presets, advancedTdpControl, caps, minW).tuning);
        setEditingAc(next === "game_ac" && acOnline);
        setSavedAcPreset(next === "game_ac" ? (ac.ac_preset ?? "") : undefined);
      }
      done(next === "game_ac" ? `${app.name} has separate battery and charger settings.`
        : `Only ${app.name} uses these settings.`);
    } catch (e: unknown) {
      if (!operationCurrent(op)) return;
      showError("scope", e);
      // A step may have landed before the failure; show what the backend kept.
      setProfileAttempt(attempt => attempt + 1);
    } finally { finishOperation(op); }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
      const result = await setPluginEnabled(checked);
      if (!operationCurrent(op)) return;
      if (!result.ok) throw new Error(result.error || "Could not change TDP control.");
      setEnabled(checked);
      done(checked ? "TDP control on." : "TDP control off. The last limits stay until the charger changes or the console restarts.");
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("enabled", e);
    } finally { finishOperation(op); }
  };

  // What runs now: the game's charger values on AC when it has them, else its battery or the global values.
  const running = perGame && acSeparate && acOnline ? acTuning : tuning;
  const runningStored = !perGame ? globalProfile.preset : acSeparate && acOnline ? savedAcPreset : savedPreset;
  const stored = absolute(running);
  const nowKey = resolvedPreset(stored.spl, stored.sppt, stored.fppt, runningStored, presets);
  // The backend names what runs now; without it (older backend) the page's own scope and values stand in.
  const nowLabel = backendRunning ? PRESET_LABELS[backendRunning.preset] : PRESET_LABELS[nowKey];
  const now = backendRunning ?? (nowKey === "custom" ? stored : presets[nowKey as Exclude<PresetKey, "custom">]);
  const runningOwner = backendRunning ? backendRunning.game : perGame;
  const enableDescription = failureFor("enabled") ?? (enabled && (profilePending || profileError) ? "Reading profile…"
    : powerSummary(enabled, runningOwner && game ? game.name : "Global", nowLabel, now,
      info?.ok ? info.values?.package_draw : null));
  const presetDescription = failureFor("preset") ?? presetNote(preset, presets);
  const scopeDescription = !game ? "" : failureFor("scope") ?? scopeNote(scope === "game_ac" ? "charger" : scope, game.name);
  const sourceDescription = sourceNote(acOnline, editingAc);

  return (
    <PanelSection title="Power">
      <PanelSectionRow>
        <ToggleField
          label="Enable TDP control"
          description={enableDescription}
          checked={enabled}
          disabled={cpuBusy || loading || !tdpAvailable}
          onChange={handleEnabledToggle}
        />
      </PanelSectionRow>
      {enabled && game && <PanelSectionRow>
        <DropdownItem
          label={`Settings for ${game.name}`}
          description={scopeDescription}
          selectedOption={scope}
          rgOptions={[{ data: "global", label: "Global" }, { data: "game", label: "This game" },
            { data: "game_ac", label: "This game + charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => { if (option.data !== scope && !cpuBusyOwnersRef.current.size) askDiscard(() => void applyScope(option.data)); }}
        />
      </PanelSectionRow>}
      {enabled && game && perGame && acSeparate && <PanelSectionRow>
        <DropdownItem
          label="Edit settings for"
          description={sourceDescription}
          selectedOption={editingAc ? "charger" : "battery"}
          rgOptions={[{ data: "battery", label: "Battery" }, { data: "charger", label: "Charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => {
            if ((option.data === "charger") === editingAc || cpuBusyOwnersRef.current.size) return;
            askDiscard(() => { if (!cpuBusyOwnersRef.current.size) setEditingAc(option.data === "charger"); });
          }}
        />
      </PanelSectionRow>}
      {enabled && PRESET_ORDER.map(key => (
        <PanelSectionRow key={key}>
          <ButtonItem
            layout="below"
            description={preset === key ? presetDescription : undefined}
            disabled={preset === key || loading || !tdpAvailable}
            onClick={() => void handlePresetChange(key)}
          >
            {preset === key ? `> ${PRESET_LABELS[key]}` : PRESET_LABELS[key]}
          </ButtonItem>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
};

export const customLimitsSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirty,
    setDirty, dirtyRef, draftBaseRef, active, setActive, caps, minW, advancedTdpControl, loading,
    tdpAvailable, operationLoading, editingAc, perGame, game, acSeparate, setTuning, setAcTuning, setSavedPreset,
    setSavedAcPreset, setGlobalProfile } = s;
  const om      = offsetMax(active.spl, caps);
  const editActive = (next: Tuning) => {
    if (!dirtyRef.current) draftBaseRef.current = { ac: editingAc, tuning: active };
    dirtyRef.current = true;
    setDirty(true);
    setActive(next);
  };
  const handlers = {
    onSpl: (v: number) => editActive(withSpl(active, v, caps, minW)),
    onSppt: (v: number) => editActive(withSppt(active, v, caps)),
    onFppt: (v: number) => editActive(withFppt(active, v, caps)),
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    const op = beginOperation();
    if (!op) return;
    const appId = (perGame && game) ? game.appId : "";
    const target = advancedTdpControl ? active : simpleTuning(active.spl, caps, minW);
    const a = absolute(target);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (!operationCurrent(op)) return;
        if (!r.ok) throw new Error(r.error || "The limits were not saved.");
        setAcTuning(target);
        setSavedAcPreset("custom");
        clearDraft();
        done(`Saved for ${game!.name} on charger.`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom",
          op.appId);
        if (!operationCurrent(op)) return;
        if (!r.ok) throw new Error(r.error || "The limits were not applied.");
        setTuning(target);
        if (!appId) setGlobalProfile({ ...a, preset: "custom" });
        else setSavedPreset("custom");
        clearDraft();
        done(appId ? `Saved for ${game!.name}.` : "Custom limits applied.");
      }
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("apply", e);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title={`Custom limits${dirty ? " · unsaved" : ""}`}>
      <PanelSectionRow>
        <SliderField
          label={`Sustained power · ${active.spl} W`}
          value={active.spl} min={minW} max={caps.spl} step={1}
          disabled={loading || !tdpAvailable}
          onChange={advancedTdpControl ? handlers.onSpl
            : (value) => editActive(simpleTuning(value, caps, minW))}
          description={advancedTdpControl ? undefined : "Boost follows automatically."}
        />
      </PanelSectionRow>
      {advancedTdpControl && <>
      <PanelSectionRow>
        <SliderField
          label={`Slow boost · +${active.spptOff} W`}
          value={active.spptOff} min={0} max={om.sppt || 1} step={1}
          disabled={om.sppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onSppt}
          description={boostRoomNote(active.spl, active.spptOff, om.sppt)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label={`Fast boost · +${active.fpptOff} W`}
          value={active.fpptOff} min={0} max={om.fppt || 1} step={1}
          disabled={om.fppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onFppt}
          description={boostRoomNote(active.spl, active.fpptOff, om.fppt)}
        />
      </PanelSectionRow>
      </>}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={apply} disabled={loading || !tdpAvailable}
          description={failureFor("apply") ?? (dirty ? "Unsaved changes" : undefined)}>
          {operationLoading ? "Applying…"
            : editingAc && game ? `Apply for ${game.name} on charger`
            : perGame && game ? `Apply for ${game.name}`
            : "Apply"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

export const moreSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, setFailure,
    readCaps, adoptCaps, capsNow, advancedTdpControl, setAdvancedTdpControl,
    caps, minW, presets, tuning, setTuning, acTuning, setAcTuning, perGame,
    setPerGame, setProfileExists, profileExists, setAcSeparate, setEditingAc, game, enabled,
    savedPreset, setSavedPreset, savedAcPreset, setSavedAcPreset, globalProfile, setGlobalProfile,
    adoptGlobalSettings, loading, tdpAvailable, operationLoading, details, setDetails, info, cpuNote } = s;
  const deleteProfile = async () => {
    if (!game) return;
    const op = beginOperation();
    if (!op) return;
    const app = game;
    try {
      const deleted = await deleteGameProfile(app.appId);
      if (!operationCurrent(op)) return;
      if (!deleted.ok) throw new Error(deleted.error || "Could not delete the profile.");
      setPerGame(false); setProfileExists(false); setAcSeparate(false); setEditingAc(false);
      setSavedPreset(undefined); setSavedAcPreset(undefined);
      const s = await unwrap(getSettings());
      if (!operationCurrent(op)) return;
      adoptGlobalSettings(s);
      done(`Profile for ${app.name} deleted.`);
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("delete", e);
    } finally { finishOperation(op); }
  };

  const handleAdvancedTdpControlToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
    try {
      const result = await setAdvancedTdpControlCall(checked);
      if (!operationCurrent(op)) return;
      if (!result.ok) throw new Error(result.error || "Could not change Separate boost limits.");
      setAdvancedTdpControl(checked);
      setFailure(null);
      clearDraft();
    } catch (error) {
      if (operationCurrent(op)) showError("advanced", error);
      return;
    }
    if (!checked) {
      const battery = absolute(tuning), charging = absolute(acTuning);
      if (resolvedPreset(battery.spl, battery.sppt, battery.fppt,
          perGame ? savedPreset : globalProfile.preset, presets) === "custom")
        setTuning(simpleTuning(tuning.spl, caps, minW));
      if (resolvedPreset(charging.spl, charging.sppt, charging.fppt,
          savedAcPreset, presets) === "custom")
        setAcTuning(simpleTuning(acTuning.spl, caps, minW));
      setGlobalProfile((current) => {
        if (resolvedPreset(current.spl, current.sppt, current.fppt, current.preset, presets) !== "custom")
          return current;
        return { ...absolute(simpleTuning(current.spl,
          caps, minW)), preset: current.preset };
      });
    } else {
      try {
        const settings = await unwrap(getSettings());
        if (!operationCurrent(op)) return;
        const global = fromAbsolute(toW(settings.spl), toW(settings.sppt), toW(settings.fppt));
        setGlobalProfile({ ...absolute(global), preset: settings.active_preset });
        if (perGame && game) {
          const appId = game.appId;
          const profile = await unwrap(getGameProfile(appId));
          if (!operationCurrent(op)) return;
          if (profile.exists) {
            const p = profile.profile;
            const ac = profile.ac_profile;
            setTuning(fromAbsolute(toW(p.spl), toW(p.sppt), toW(p.fppt)));
            setAcTuning(fromAbsolute(toW(ac.spl), toW(ac.sppt), toW(ac.fppt)));
          }
        } else {
          setTuning(global);
        }
      } catch (error) {
        if (operationCurrent(op)) showError("advanced", error);
      }
    }
    } finally { finishOperation(op); }
  };

  const handleHelperRetry = async () => {
    const op = beginOperation(false, false);
    if (!op) return;
    try {
      const result = await retryHelper();
      if (!operationCurrent(op)) return;
      if (!result.ok) throw new Error(result.error || "Retry failed");
      const c = await readCaps();
      if (operationCurrent(op)) { adoptCaps(c); setFailure(null); }
    } catch (error) {
      if (operationCurrent(op)) showError("helper", error);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title="More">
      <PanelSectionRow>
        <ToggleField
          label="Separate boost limits"
          description={failureFor("advanced") ?? "Set slow and fast boost yourself in Custom."}
          checked={advancedTdpControl}
          disabled={loading || !tdpAvailable}
          onChange={handleAdvancedTdpControlToggle}
        />
      </PanelSectionRow>
      {enabled && game && profileExists && <PanelSectionRow>
        <ButtonItem layout="below" disabled={loading} description={failureFor("delete")}
          onClick={() => confirmAction(`Delete profile for ${game.name}?`,
            `Its power, CPU Boost and energy settings are removed. ${game.name} will use Global settings.`,
            "Delete", () => void deleteProfile(), { destructive: true })}>
          {`Delete profile for ${game.name}`}
        </ButtonItem>
      </PanelSectionRow>}
      {capsNow.helperRetryAvailable && <PanelSectionRow>
        <ButtonItem layout="below" disabled={operationLoading} description={failureFor("helper")}
          onClick={handleHelperRetry}>Retry helper download</ButtonItem>
      </PanelSectionRow>}
      {detailsRows(details, () => setDetails(value => !value), [
        // The kernel lets RyzenAdj read the limits back only with iomem=relaxed.
        ...(capsNow.readback ? [["Limits now", limitsText(info)] as [string, string]] : []),
        ["Package power", drawText(info)],
        ["Set by", "RyzenAdj"],
        ...(cpuNote ? [["CPU energy", cpuNote] as [string, string]] : []),
      ])}
    </PanelSection>
  );
};
