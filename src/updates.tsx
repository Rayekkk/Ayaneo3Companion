// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, toaster } from "@decky/api";
import { Field, PanelSection, PanelSectionRow, Spinner } from "@decky/ui";
import { FC, useCallback, useEffect, useState } from "react";
import { settle, type Result } from "./result";
import { StackedAction } from "./ui";

interface UpdateInfo extends Result<never> {
  current_version?: string;
  latest_version?: string;
  update_available?: boolean;
  download_available?: boolean;
  asset_name?: string;
  no_release?: boolean;
}

const getVersion = callable<[], Result<never> & { version?: string }>("get_version");
const checkForUpdates = callable<[], UpdateInfo>("check_for_updates");
const performUpdate = callable<[string], Result<never> & { version?: string; path?: string; sha256?: string }>("perform_update");

interface UpdateView { info: UpdateInfo | null; checking: boolean; downloading: boolean; path: string | null }
let updateView: UpdateView = { info: null, checking: false, downloading: false, path: null };
let updateGeneration = 0;
const updateListeners = new Set<(value: UpdateView) => void>();
const publishUpdate = (next: Partial<UpdateView>) => {
  updateView = { ...updateView, ...next };
  updateListeners.forEach(listener => listener(updateView));
};

export const resetUpdates = () => {
  ++updateGeneration;
  updateListeners.clear();
  updateView = { info: null, checking: false, downloading: false, path: null };
};

export const UpdateSection: FC<{ initialVersion: string }> = ({ initialVersion }) => {
  const [view, setView] = useState(updateView);
  const { info: updateInfo, checking, downloading, path: downloadPath } = view;
  const [version, setVersion] = useState(initialVersion);

  useEffect(() => {
    let active = true;
    updateListeners.add(setView);
    setView(updateView);
    void settle(getVersion()).then(result => { if (active && result.version) setVersion(result.version); });
    return () => { active = false; updateListeners.delete(setView); };
  }, []);

  const notifyFailure = (title: string, error: unknown) => {
    const body = error instanceof Error ? error.message : String(error ?? "Unknown error");
    try { toaster.toast({ title, body, duration: 4000 }); }
    catch { console.error(`[ayaneo3companion] ${title}: ${body}`); }
  };

  const check = useCallback(async () => {
    if (updateView.checking || updateView.downloading) return;
    const generation = updateGeneration;
    publishUpdate({ checking: true, info: null, path: null });
    try {
      const info = await checkForUpdates();
      if (generation === updateGeneration) publishUpdate({ info });
    } catch (error) {
      if (generation === updateGeneration) {
        notifyFailure("Update check failed", error);
        publishUpdate({ info: { ok: false, error: error instanceof Error ? error.message : String(error) } });
      }
    } finally {
      if (generation === updateGeneration) publishUpdate({ checking: false });
    }
  }, []);

  const download = useCallback(async () => {
    if (updateView.checking || updateView.downloading) return;
    const generation = updateGeneration;
    const expectedVersion = updateView.info?.latest_version;
    if (!expectedVersion) return;
    publishUpdate({ downloading: true });
    try {
      const result = await performUpdate(expectedVersion);
      if (generation !== updateGeneration) return;
      if (result.ok && result.path && result.version === expectedVersion) publishUpdate({ path: result.path });
      else if (result.ok) throw new Error("The downloaded release does not match the selected version. Check for updates again.");
      else throw new Error(result.error ?? "Unknown error");
    } catch (error) {
      if (generation === updateGeneration) {
        publishUpdate({ info: { ...(updateView.info ?? { ok: false }), error: error instanceof Error ? error.message : String(error) } });
        notifyFailure("Download failed", error);
      }
    } finally {
      if (generation === updateGeneration) publishUpdate({ downloading: false });
    }
  }, []);

  const versionTag = (value: string) => (
    <span style={{ fontWeight: 700, color: "var(--gpColor-White, #fff)", background: "rgba(102,192,244,.18)", borderRadius: "4px", padding: "1px 6px" }}>
      v{value}
    </span>
  );

  // A release on offer that cannot be downloaded says why ("message") where the error was shown before;
  // no published release at all still reads "Up to date".
  const problem = updateInfo?.error || (updateInfo?.no_release ? undefined : updateInfo?.message);

  return <PanelSection title="Updates">
    <PanelSectionRow>
      <div style={{ width: "100%", minWidth: 0, fontSize: "12px", color: "var(--gpColor-TextMuted, rgba(255,255,255,.6))", overflowWrap: "anywhere" }}>
        Installed: {versionTag((updateInfo?.current_version ?? version) || "?")}
        {updateInfo?.latest_version && !problem && <>
          {" · "}Latest: {versionTag(updateInfo.latest_version)}
        </>}
      </div>
    </PanelSectionRow>
    {problem && <PanelSectionRow>
      <div style={{ width: "100%", minWidth: 0, padding: "8px", boxSizing: "border-box", borderRadius: "4px", background: "rgba(248,113,113,.14)", color: "var(--gpColor-Red, #f87171)", overflowWrap: "anywhere" }}>
        {problem}
      </div>
    </PanelSectionRow>}
    {updateInfo && !problem && !updateInfo.update_available && !downloadPath && <PanelSectionRow>
      <div style={{ fontSize: "12px", color: "var(--gpColor-Green, #4ade80)" }}>Up to date</div>
    </PanelSectionRow>}
    {updateInfo?.update_available && updateInfo.download_available && updateInfo.asset_name && !downloadPath && <StackedAction
      title={`Version ${updateInfo.latest_version} is available`}
      description="Download the exact GitHub release ZIP to the SteamOS Downloads folder."
      disabled={downloading || checking}
      onClick={() => void download()}
    >{downloading ? <Spinner /> : `Download v${updateInfo.latest_version}`}</StackedAction>}
    {downloadPath && <PanelSectionRow>
      <Field focusable
        label="Update downloaded"
        description={<span>Saved to <span style={{ fontFamily: "monospace", overflowWrap: "anywhere" }}>{downloadPath}</span><br /><br />To install: Decky → Settings → Developer → Uninstall AYANEO 3 Companion → Install Plugin from ZIP. Settings and per-game profiles are preserved.</span>}
      />
    </PanelSectionRow>}
    <StackedAction
      title="Check for Updates"
      description="Compare the installed version with the latest GitHub release."
      disabled={checking || downloading}
      onClick={() => void check()}
    >{checking ? <Spinner /> : "Check"}</StackedAction>
  </PanelSection>;
};
