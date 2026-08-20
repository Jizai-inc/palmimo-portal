import { describe, expect, it } from "vitest";

import { formatUtcTimestamp } from "@/lib/formatTimestamp";

describe("formatUtcTimestamp", () => {
  it("renders a fixed epoch as an exact UTC string, regardless of the runner's local timezone", () => {
    // 2026-08-20T10:31:00Z
    const timestamp = Date.UTC(2026, 7, 20, 10, 31, 0) / 1000;

    expect(formatUtcTimestamp(timestamp)).toBe("2026-08-20 10:31 UTC");
  });

  it("omits the time when withTime is false", () => {
    const timestamp = Date.UTC(2026, 7, 20, 10, 31, 0) / 1000;

    expect(formatUtcTimestamp(timestamp, { withTime: false })).toBe("2026-08-20 UTC");
  });

  it("omits the year when withYear is false", () => {
    const timestamp = Date.UTC(2026, 7, 20, 10, 31, 0) / 1000;

    expect(formatUtcTimestamp(timestamp, { withYear: false })).toBe("08-20 10:31 UTC");
  });

  it("renders in UTC even when the local calendar day would differ (near-midnight UTC)", () => {
    // 2026-01-01T23:30:00Z -- in a local timezone ahead of UTC, the naive
    // local-time formatting this replaced would show 2026-01-02 instead.
    const timestamp = Date.UTC(2026, 0, 1, 23, 30, 0) / 1000;

    expect(formatUtcTimestamp(timestamp)).toBe("2026-01-01 23:30 UTC");
  });

  it("renders a placeholder for a non-finite timestamp instead of an Invalid Date string", () => {
    expect(formatUtcTimestamp(NaN)).toBe("--");
    expect(formatUtcTimestamp(Infinity)).toBe("--");
    expect(formatUtcTimestamp(-Infinity)).toBe("--");
  });
});
