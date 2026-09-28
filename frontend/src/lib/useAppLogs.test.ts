import { describe, expect, it } from "vitest";

import { LOG_HISTORY_CAP, appendCapped } from "@/lib/useAppLogs";
import type { JournalEntryInfo } from "@/api/generated/models";

function entries(count: number, offset = 0): JournalEntryInfo[] {
  return Array.from({ length: count }, (_, i) => ({ invocation_id: null, message: `line-${offset + i}`, timestamp: null }));
}

describe("appendCapped", () => {
  it("keeps every entry and stays untruncated while under the cap", () => {
    const result = appendCapped({ entries: entries(3), truncated: false }, entries(2, 3), 10);

    expect(result.entries).toHaveLength(5);
    expect(result.truncated).toBe(false);
  });

  it("drops the oldest entries once the total exceeds the cap, keeping the newest", () => {
    const result = appendCapped({ entries: entries(LOG_HISTORY_CAP - 1), truncated: false }, entries(5, LOG_HISTORY_CAP - 1), LOG_HISTORY_CAP);

    expect(result.entries).toHaveLength(LOG_HISTORY_CAP);
    expect(result.truncated).toBe(true);
    expect(result.entries[0].message).toBe("line-4");
    expect(result.entries.at(-1)?.message).toBe(`line-${LOG_HISTORY_CAP + 3}`);
  });

  it("stays truncated once it has ever truncated, even if the next append is small", () => {
    const result = appendCapped({ entries: entries(5), truncated: true }, entries(1, 5), 10);

    expect(result.truncated).toBe(true);
  });
});
