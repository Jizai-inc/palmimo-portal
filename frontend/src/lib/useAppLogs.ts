import { useEffect, useState } from "react";

import { useGetLogsApiV1AppsNameLogsGet } from "@/api/generated/apps/apps";
import type { JournalEntryInfo } from "@/api/generated/models";

/** How often to re-poll logs while the app is running (design doc 3.7). */
export const LOG_POLL_INTERVAL_MS = 2_000;
const DEFAULT_LOG_LINES = 200;

export interface UseAppLogsResult {
  unavailable: "journal_permission" | null | undefined;
  invocations: string[];
  invocation: string | null;
  setInvocation: (invocation: string | null) => void;
  accumulated: JournalEntryInfo[];
  text: string;
  refetch: () => void;
  /** Earliest known timestamp among the shown invocation's loaded entries, or `null` if none carry one yet. */
}

/**
 * Fetches, accumulates, and polls one app's logs -- shared by the detail screen's inline logs
 * section and the full-page logs view so the cursor-paging and invocation-switching logic
 * (design doc 3.2/3.8) exists in exactly one place.
 */
export function useAppLogs(name: string, status: string): UseAppLogsResult {
  const [invocation, setInvocation] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [accumulated, setAccumulated] = useState<JournalEntryInfo[]>([]);
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

  // Switching invocations starts a fresh cursor/accumulation -- the previous invocation's
  // entries are a different journal window, not a continuation.
  useEffect(() => {
    setCursor(null);
    setAccumulated([]);
  }, [invocation]);

  // Cursor paging: each response's `next_cursor` tails forward from where the last one left
  // off, so the next poll (or a manual "load more" while not polling) only carries the entries
  // since then, appended rather than replacing what's already shown.
  useEffect(() => {
    if (!logs || logs.unavailable) return;
    setAccumulated((current) => [...current, ...(logs.entries ?? [])]);
    if (logs.next_cursor) {
      setCursor(logs.next_cursor);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logs]);

  return {
    unavailable: logs?.unavailable,
    invocations: logs?.invocations ?? [],
    invocation,
    setInvocation,
    accumulated,
    text: accumulated.map((entry) => entry.message).join("\n"),
    refetch: () => void refetch(),
  };
}
