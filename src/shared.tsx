// SPDX-License-Identifier: BSD-3-Clause
// Helpers shared by the Decky pages. Render helpers are plain functions (not
// components) so the element tree is exactly what the page used to inline.
import { ButtonItem, ConfirmModal, Field, findModuleExport, PanelSectionRow, Router, showModal } from "@decky/ui";
import { Fragment, type ReactNode } from "react";
import type { Result } from "./result";

/** A read-only row; `focusable` keeps it reachable with the D-pad. */
export const infoRow = (label: ReactNode, description?: ReactNode) =>
  <PanelSectionRow><Field focusable label={label} description={description} /></PanelSectionRow>;

/**
 * Ask first; only for actions that close apps, can drop Wi-Fi, delete data or raise hardware limits.
 * The dialog takes focus from the Quick Access menu, so `onOK` usually runs while the page is hidden:
 * gate it on what the dialog showed, not on visibility, and report its outcome with a toast.
 */
export const confirmAction = (title: string, description: string, ok: string, onOK: () => void) =>
  showModal(<ConfirmModal strTitle={title} strDescription={description} strOKButtonText={ok}
    strCancelButtonText="Cancel" onOK={onOK} />);

/**
 * A page's last rows: "Show details" and, below it, rare repair actions and then
 * the technical facts, so the last row stays a focusable, always-enabled field.
 */
export const detailsRows = (shown: boolean, toggle: () => void, rows: Array<[ReactNode, ReactNode]>, actions?: ReactNode) => <>
  <PanelSectionRow><ButtonItem layout="below" onClick={toggle}>{shown ? "Hide details" : "Show details"}</ButtonItem></PanelSectionRow>
  {shown && actions}
  {shown && rows.map(([label, description], index) => <Fragment key={index}>{infoRow(label, description)}</Fragment>)}
</>;

/** An Error's message; anything else as the fallback, or as text when there is none. */
export const errorText = (error: unknown, fallback?: string) =>
  error instanceof Error ? error.message : fallback ?? String(error);

/** The state of a D2 answer; a refusal throws its message, as the call rejected before the D2 contract. */
export const unwrap = <T,>(call: Promise<Result<T>>): Promise<T> => call.then(r => {
  if (!r?.ok) throw new Error(r?.error || "Unknown error");
  return r.state as T;
});

// ── Running app watcher ───────────────────────────────────────────────────────

export type RunningApp = { appid: string | number; display_name?: string };
/** A module fed by the game watcher; it keeps its own state, gating and RPC. */
type GameClient = { check(app: RunningApp | null): Promise<void>; resume(): void };

const gameClients = new Set<GameClient>();
let gameGeneration = 0;
let gameTimer: ReturnType<typeof setInterval> | undefined;
let gameUnsubs: Array<() => void> = [];
const lateChecks = new Set<ReturnType<typeof setTimeout>>();

/** Router.MainRunningApp, or null when nothing runs or the router is unavailable. */
export const runningApp = (): RunningApp | null => {
  try {
    const app = (Router as any)?.MainRunningApp;
    return app?.appid ? app : null;
  } catch {
    return null;
  }
};

/** One Router read, handed to every watching module. */
const checkRunningApp = () => {
  const app = runningApp();
  return Promise.all([...gameClients].map((client) => client.check(app)));
};

/** Steam's client API, read from window as before; undefined outside Steam. */
const steamClient = () => (window as { SteamClient?: typeof SteamClient }).SteamClient;

/**
 * Subscribe to resume-from-suspend. Returns an unsubscribe function, or null
 * when the client offers no way to hear about it.
 *
 * `SteamClient.System.RegisterForOnResumeFromSuspend` was removed from the
 * Steam client in the September 2025 beta. Optional chaining meant calling it
 * silently did nothing - confirmed on the device, where two suspend cycles
 * produced no callback and no error. The replacement lives on a SleepManager
 * module, reachable either as a global or through the webpack exports; the
 * legacy call stays as a fallback for older clients.
 */
function onResumeFromSuspend(handler: () => void): (() => void) | null {
  const asUnsub = (reg: SteamUnregister | null | undefined): (() => void) | null => {
    if (typeof reg === "function") return reg;
    if (typeof reg?.unregister === "function") return () => reg.unregister();
    return null;
  };
  const isSleepManager = (e: any) =>
    !!e && typeof e === "object" &&
    (typeof e.RegisterForNotifyResumeFromSuspend === "function" ||
      typeof e.NotifyResumeFromSuspend === "function");

  try {
    const mgr: SteamSleepManager | undefined = (window as { SleepManager?: SteamSleepManager }).SleepManager
      ?? findModuleExport(isSleepManager);
    const unsub = asUnsub(mgr?.RegisterForNotifyResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[ayaneo3companion] game_watcher.resume_lookup_failed");
  }

  try {
    const unsub = asUnsub(
      steamClient()?.System?.RegisterForOnResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[ayaneo3companion] game_watcher.legacy_resume_registration_failed");
  }

  // Said out loud rather than swallowed: this going quiet again is exactly how
  // the previous registration rotted unnoticed.
  console.warn("[ayaneo3companion] no resume-from-suspend notification available; "
    + "TDP and vibration will be restored by the backend loops instead");
  return null;
}

function startGameWatch() {
  const generation = ++gameGeneration;
  const live = () => generation === gameGeneration;
  const steam = steamClient();
  try {
    const reg = steam?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
      if (!live()) return;
      // Router.MainRunningApp lags the notification slightly.
      const timer = setTimeout(() => {
        lateChecks.delete(timer);
        if (live()) void checkRunningApp();
      }, 300);
      lateChecks.add(timer);
    });
    if (reg && typeof reg !== "function" && reg.unregister) gameUnsubs.push(() => reg.unregister());
  } catch (e) {
    console.warn("[ayaneo3companion] game_watcher.app_notifications_unavailable");
  }
  // Decky has no backend resume hook, so this notification is the only way to
  // restore SMU limits and controller settings before the backend loops notice.
  const offResume = onResumeFromSuspend(() => {
    if (live()) gameClients.forEach((client) => client.resume());
  });
  if (offResume) gameUnsubs.push(offResume);
  // Router.MainRunningApp can change without a lifetime event firing.
  gameTimer = setInterval(() => void checkRunningApp(), 2000);
}

function stopGameWatch() {
  gameGeneration += 1;
  for (const timer of lateChecks) clearTimeout(timer);
  lateChecks.clear();
  if (gameTimer) clearInterval(gameTimer);
  gameTimer = undefined;
  for (const off of gameUnsubs) {
    try {
      off();
    } catch {
      /* the subscription may already be gone */
    }
  }
  gameUnsubs = [];
}

/**
 * Feed `client` the foreground app: now, every 2 s and after app lifetime
 * events, plus resume-from-suspend. One timer and one set of Steam
 * subscriptions serve every module. Returns the unsubscribe.
 */
export function watchRunningApp(client: GameClient): () => void {
  if (!gameClients.size) startGameWatch();
  gameClients.add(client);
  void client.check(runningApp());
  return () => {
    if (gameClients.delete(client) && !gameClients.size) stopGameWatch();
  };
}
