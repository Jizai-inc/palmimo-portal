import { describe, expect, it } from "vitest";

import { formatCheckedAt, formatReleaseDate } from "@/lib/updateFormat";

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
