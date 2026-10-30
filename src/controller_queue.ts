// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// The controller's write queue (vibration, lighting, FF Gain): edits merge into one patch sent after a short delay,
// one drain at a time; closing the menu sends what is pending at once.

import { callable } from "@decky/api";
import { useRef } from "react";
import type { Result } from "./result";
import { unwrap } from "./shared";
import type { Controller, RgbMode, State } from "./model";

const setController = callable<[Partial<Controller>], Result<State>>("set_controller");
const setControllerWithVibrationFeedback = callable<[Partial<Controller>], Result<State>>("set_controller_with_vibration_feedback");
const setVibrationGain = callable<[number], Result<State>>("set_vibration_gain");

/** What the queue needs from the page; read anew at every use. */
export interface QueueHost {
  mounted: { current: boolean };
  /** Bumped by every write so an older status read is dropped. */
  revision: { current: number };
  /** Another action runs or the backend is not ready: an edit is ignored. */
  blocked(): boolean;
  /** The controller settings the page shows now. */
  shown(): Controller;
  /** An edit before it is sent (only while mounted). */
  show(controller: Controller): void;
  /** The backend confirmed the drained edits (only while mounted). */
  applied(state: State): void;
  failed(message: string): void;
  refresh(): void;
}

export function useControllerQueue(host: QueueHost) {
  const hostRef = useRef(host);
  hostRef.current = host;
  const dirty = useRef(false);
  const pending = useRef<Controller | null>(null);
  const patchRef = useRef<Partial<Controller>>({});
  const writing = useRef(false);
  const drainRef = useRef<Promise<boolean> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const desired = useRef<Controller | null>(null);
  const lastRgbMode = useRef<RgbMode>("solid");

  const flush = (): Promise<boolean> => {
    if (drainRef.current) return drainRef.current;
    if (!pending.current) return Promise.resolve(true);
    const page = hostRef.current;
    writing.current = true;
    let needsRefresh = false;
    const unconfirmed = new Set<keyof Controller>();
    let resolveDrain!: (value: boolean) => void;
    let rejectDrain!: (error: unknown) => void;
    const drain = new Promise<boolean>((resolve, reject) => { resolveDrain = resolve; rejectDrain = reject; });
    drainRef.current = drain;
    ++page.revision.current;
    // Share completion of the entire queue, including its in-flight write,
    // while still starting the first RPC immediately when QAM is closed.
    const processQueue = async () => {
      try {
        while (pending.current) {
          pending.current = null;
          const patch = patchRef.current;
          patchRef.current = {};
          const firmwareChanged = patch.vibration !== undefined || patch.rgb_mode !== undefined
            || patch.color !== undefined || patch.brightness !== undefined;
          try {
            let applied: State | null = null;
            if (firmwareChanged) {
              applied = await unwrap(patch.vibration !== undefined
                ? setControllerWithVibrationFeedback(patch)
                : setController(patch));
            }
            // Mixed edits commit firmware and FF_GAIN in one transaction;
            // gain alone still avoids an unnecessary HID write.
            if (patch.ff_gain !== undefined && !firmwareChanged) applied = await unwrap(setVibrationGain(patch.ff_gain));
            // A later independent patch does not confirm fields that failed
            // earlier in this drain. An explicit newer edit of that same field
            // can still supersede the failure and become confirmed.
            for (const field of Object.keys(patch) as Array<keyof Controller>) unconfirmed.delete(field);
            if (!pending.current) {
              dirty.current = false;
              if (applied && hostRef.current.mounted.current) hostRef.current.applied(applied);
            }
          } catch (error) {
            for (const field of Object.keys(patch) as Array<keyof Controller>) unconfirmed.add(field);
            hostRef.current.failed(error instanceof Error ? error.message : String(error));
            if (!pending.current) {
              dirty.current = false;
              desired.current = null;
              needsRefresh = true;
            }
          }
        }
      } finally {
        ++hostRef.current.revision.current;
        writing.current = false;
        drainRef.current = null;
        if (pending.current) void flush();
        else if (needsRefresh && hostRef.current.mounted.current) hostRef.current.refresh();
      }
      return unconfirmed.size === 0;
    };
    void processQueue().then(resolveDrain, rejectDrain);
    return drain;
  };

  /** The controller with every edit so far, sent or not. */
  const current = () => pending.current ?? desired.current ?? hostRef.current.shown();

  const apply = (part: Partial<Controller>, delay = 0) => {
    const page = hostRef.current;
    if (page.blocked()) return;
    ++page.revision.current;
    const controller = { ...current(), ...part };
    if (controller.rgb_mode !== "off") lastRgbMode.current = controller.rgb_mode;
    dirty.current = true;
    pending.current = controller;
    patchRef.current = { ...patchRef.current, ...part };
    desired.current = controller;
    if (page.mounted.current) page.show(controller);
    if (timer.current) clearTimeout(timer.current);
    if (!page.mounted.current) { timer.current = null; void flush(); }
    else timer.current = setTimeout(() => { timer.current = null; void flush(); }, delay);
  };

  const cancelTimer = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  /** Sends a debounced edit now (the menu closes or the page unmounts). */
  const flushPending = () => {
    cancelTimer();
    if (pending.current) void flush();
  };
  /** A write is queued or running: a status read would show older settings. */
  const busy = () => writing.current || pending.current !== null;

  return { dirty, desired, lastRgbMode, flush, apply, current, cancelTimer, flushPending, busy };
}
