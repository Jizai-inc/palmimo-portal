import { describe, expect, it } from "vitest";

import { formatCheckedAt, formatReleaseDate, formatUtcTimestamp } from "@/lib/updateFormat";

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

  it("renders in UTC even when the local calendar day would differ (near-midnight UTC)", () => {
    // 2026-01-01T23:30:00Z -- in a local timezone ahead of UTC, the naive
    // local-time formatting this replaced would show 2026-01-02 instead.
    const timestamp = Date.UTC(2026, 0, 1, 23, 30, 0) / 1000;

    expect(formatUtcTimestamp(timestamp)).toBe("2026-01-01 23:30 UTC");
  });
});

describe("formatCheckedAt", () => {
  it('renders "—" when there has never been a check', () => {
    expect(formatCheckedAt(null)).toBe("—");
  });

  it("renders the checked_at timestamp via the shared UTC formatter", () => {
    const timestamp = Date.UTC(2026, 7, 20, 10, 31, 0) / 1000;

    expect(formatCheckedAt(timestamp)).toBe("2026-08-20 10:31 UTC");
  });
});

describe("formatReleaseDate", () => {
  it("renders the release date in UTC with an explicit suffix and no time", () => {
    expect(formatReleaseDate("2026-08-20T10:31:00Z")).toBe("2026-08-20 UTC");
  });

  it("falls back to the raw string when it does not parse", () => {
    expect(formatReleaseDate("not-a-date")).toBe("not-a-date");
  });
});
