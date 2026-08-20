import { describe, expect, it } from "vitest";

import type { WifiAttemptInfo } from "@/api/generated/models";
import i18n from "@/i18n";
import { formatLastWifiAttempt } from "@/lib/wifiAttemptFormat";

function attempt(overrides: Partial<WifiAttemptInfo>): WifiAttemptInfo {
  return { ssid: "HomeNet", result: "connected", timestamp: 0, ...overrides };
}

describe("formatLastWifiAttempt", () => {
  it('renders "—" when there is no attempt on record', () => {
    expect(formatLastWifiAttempt(null, i18n.t.bind(i18n))).toBe("—");
    expect(formatLastWifiAttempt(undefined, i18n.t.bind(i18n))).toBe("—");
  });

  it('renders "Connecting…" while an attempt is still in flight', () => {
    expect(formatLastWifiAttempt(attempt({ result: "attempting" }), i18n.t.bind(i18n))).toBe("Connecting…");
  });

  it("renders the connected copy with a fixed-epoch, exact UTC MM-DD HH:mm timestamp", () => {
    // 2024-03-05T06:07:00Z -- a fixed epoch pinned to an exact string, so the
    // assertion does not depend on the test runner's own timezone.
    const timestamp = Date.UTC(2024, 2, 5, 6, 7, 0) / 1000;

    expect(formatLastWifiAttempt(attempt({ result: "connected", timestamp }), i18n.t.bind(i18n))).toBe(
      "Connected (03-05 06:07 UTC)",
    );
  });

  it("renders the failed copy with a fixed-epoch, exact UTC MM-DD HH:mm timestamp", () => {
    const timestamp = Date.UTC(2024, 2, 5, 6, 7, 0) / 1000;

    expect(formatLastWifiAttempt(attempt({ result: "failed", timestamp }), i18n.t.bind(i18n))).toBe(
      "Failed (03-05 06:07 UTC)",
    );
  });

  it("renders in UTC even when the local calendar day would differ (near-midnight UTC)", () => {
    // 2024-03-05T23:30:00Z -- in a local timezone ahead of UTC, the naive
    // local-time formatting this replaced would show 03-06 instead.
    const timestamp = Date.UTC(2024, 2, 5, 23, 30, 0) / 1000;

    expect(formatLastWifiAttempt(attempt({ result: "connected", timestamp }), i18n.t.bind(i18n))).toBe(
      "Connected (03-05 23:30 UTC)",
    );
  });
});
