import { useEffect, useState } from "react";

import { useGetLogsApiV1AppsNameLogsGet } from "@/api/generated/apps/apps";
import type { JournalEntryInfo, JournalInvocationInfo } from "@/api/generated/models";

/** How often to re-poll logs while the app is running (design doc 3.7). */
export const LOG_POLL_INTERVAL_MS = 2_000;
const DEFAULT_LOG_LINES = 200;

/** Cap on entries kept in memory per invocation -- an unbounded accumulation across an
 * hours-long poll would otherwise grow the tab's memory use without limit. */
export const LOG_HISTORY_CAP = 2_000;

interface LogHistory {
  entries: JournalEntryInfo[];
  /** Once true for an invocation, stays true: entries dropped to stay under the cap never come back. */
  truncated: boolean;
}

/** Append `incoming` to `current.entries`, dropping the oldest beyond `cap`. */
export function appendCapped(current: LogHistory, incoming: JournalEntryInfo[], cap: number): LogHistory {
  const merged = [...current.entries, ...incoming];
  if (merged.length <= cap) {
    return { entries: merged, truncated: current.truncated };
  }
  return { entries: merged.slice(merged.length - cap), truncated: true };
}

export interface UseAppLogsResult {
  unavailable: "journal_permission" | null | undefined;
  invocations: JournalInvocationInfo[];
  invocation: string | null;
  setInvocation: (invocation: string | null) => void;
  /**
   * True while showing the unit's newest known run rather than one the caller pinned by picking
   * an older entry from `invocations`. The backend exposes no "this is the unit's current
   * invocation" field, so this is inferred: not pinned == following the newest id `invocations`
   * reports, which is also the id a running app is presumed to be on.
   */
  isCurrentInvocation: boolean;
  accumulated: JournalEntryInfo[];
  /** True once older entries have been dropped to stay under `LOG_HISTORY_CAP`. */
  truncated: boolean;
  text: string;
  refetch: () => void;
}

/**
 * Fetches, accumulates, and polls one app's logs -- shared by the detail screen's inline logs
 * section and the full-page logs view so the cursor-paging and invocation-switching logic
 * (design doc 3.2/3.8) exists in exactly one place.
 */
/** A cursor is only valid for the invocation it was issued against -- pairing them stops a
 * cursor from a just-abandoned invocation from ever being sent alongside a new one. */
interface CursorState {
  invocation: string | null;
  cursor: string | null;
}

export function useAppLogs(name: string, status: string): UseAppLogsResult {
  // `null` means "follow the newest run" -- re-derived from the latest response's `invocations`
  // on every render, not captured once, so a run that starts or restarts while this is open is
  // picked up on the next poll instead of leaving the view pinned to whatever was newest at
  // mount/selection time. Only an explicit pick from the invocation dropdown fixes this to an id
  // -- picking the run that is already the newest known one is the same as following it, so that
  // case clears the pin instead of fixing it.
  const [pinnedInvocation, setPinnedInvocation] = useState<string | null>(null);
  const [cursorState, setCursorState] = useState<CursorState>({ invocation: null, cursor: null });
  const [history, setHistory] = useState<LogHistory>({ entries: [], truncated: false });
  // Mirrors the query's own `data.invocations`, one render behind: computing this render's
  // `invocation` (which the query below is parameterized on) from this render's own query result
  // would be circular, so "follow" reads the previous response's list instead. An effect syncing
  // this after each response converges within one extra poll of a run starting or restarting.
  // Never synced to an empty list: `_list_invocations` answers `[]` when its own journalctl call
  // fails, which would otherwise make "follow" lose the run it was already showing over a single
  // transient failure.
  const [knownInvocations, setKnownInvocations] = useState<JournalInvocationInfo[]>([]);

  const invocation = pinnedInvocation ?? knownInvocations[0]?.id ?? null;
  // A cursor from a different invocation is stale the moment `invocation` changes -- computed
  // rather than cleared by an effect, so the very next request (not one render later) already
  // omits it instead of pairing a new invocation with an old cursor.
  const cursor = cursorState.invocation === invocation ? cursorState.cursor : null;

  const setInvocation = (id: string | null) => {
    setPinnedInvocation(id !== null && id === (knownInvocations[0]?.id ?? null) ? null : id);
  };

  // The generated client serializes an explicit `null` param as the literal query string
  // "null" rather than omitting it (`getGetLogsApiV1AppsNameLogsGetUrl`'s
  // `value === null ? 'null' : String(value)`), which the backend would bind as that literal
  // string, not "absent" -- so a null cursor/invocation is left out of the params object
  // entirely instead of passed through.
  const { data: logs, refetch } = useGetLogsApiV1AppsNameLogsGet(
    name,
    { lines: DEFAULT_LOG_LINES, ...(invocation !== null ? { invocation } : {}), ...(cursor !== null ? { cursor } : {}) },
    { query: { refetchInterval: status === "running" ? LOG_POLL_INTERVAL_MS : false } },
  );

  useEffect(() => {
    if (logs?.invocations && logs.invocations.length > 0) setKnownInvocations(logs.invocations);
  }, [logs?.invocations]);

  // A pinned invocation that falls out of the last 20 (`invocations`) would otherwise leave the
  // dropdown showing a selection that no longer matches any option -- fall back to following.
  useEffect(() => {
    if (pinnedInvocation !== null && logs?.invocations && logs.invocations.length > 0) {
      if (!logs.invocations.some((candidate) => candidate.id === pinnedInvocation)) {
        setPinnedInvocation(null);
      }
    }
  }, [pinnedInvocation, logs?.invocations]);

  // Switching invocations starts a fresh accumulation -- the previous invocation's entries are a
  // different journal window, not a continuation.
  useEffect(() => {
    setHistory({ entries: [], truncated: false });
  }, [invocation]);

  // Cursor paging: each response's `next_cursor` tails forward from where the last one left
  // off, so the next poll (or a manual "load more" while not polling) only carries the entries
  // since then, appended (capped at LOG_HISTORY_CAP) rather than replacing what's already shown.
  //
  // A response fetched with no invocation filter (mount, before the first `invocations` list has
  // arrived) can mix several runs' tails together -- committed only when it in fact carries at
  // most one distinct invocation, which covers the ordinary single-run case without ever
  // rendering a mixed batch.
  useEffect(() => {
    if (!logs || logs.unavailable) return;
    const entries = logs.entries ?? [];
    const mixed =
      invocation !== null
        ? entries.some((entry) => entry.invocation_id !== invocation)
        : new Set(entries.map((entry) => entry.invocation_id)).size > 1;
    if (mixed) return;
    setHistory((current) => appendCapped(current, entries, LOG_HISTORY_CAP));
    setCursorState({ invocation, cursor: logs.next_cursor ?? null });
  }, [logs, invocation]);

  return {
    unavailable: logs?.unavailable,
    invocations: logs?.invocations ?? [],
    invocation,
    setInvocation,
    isCurrentInvocation: pinnedInvocation === null,
    accumulated: history.entries,
    truncated: history.truncated,
    text: history.entries.map((entry) => entry.message).join("\n"),
    refetch: () => void refetch(),
  };
}
