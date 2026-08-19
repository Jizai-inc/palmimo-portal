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

  it("renders the connected copy with a MM-DD HH:mm timestamp", () => {
    // 2024-03-05T06:07:00Z -- use a UTC-based Date so the assertion does not
    // depend on the test runner's own timezone.
    const timestamp = Date.UTC(2024, 2, 5, 6, 7, 0) / 1000;
    const expectedDate = new Date(timestamp * 1000);
    const pad = (n: number) => String(n).padStart(2, "0");
    const expected = `Connected (${pad(expectedDate.getMonth() + 1)}-${pad(expectedDate.getDate())} ${pad(expectedDate.getHours())}:${pad(expectedDate.getMinutes())})`;

    expect(formatLastWifiAttempt(attempt({ result: "connected", timestamp }), i18n.t.bind(i18n))).toBe(expected);
  });

  it("renders the failed copy with a MM-DD HH:mm timestamp", () => {
    const timestamp = Date.UTC(2024, 2, 5, 6, 7, 0) / 1000;
    const expectedDate = new Date(timestamp * 1000);
    const pad = (n: number) => String(n).padStart(2, "0");
    const expected = `Failed (${pad(expectedDate.getMonth() + 1)}-${pad(expectedDate.getDate())} ${pad(expectedDate.getHours())}:${pad(expectedDate.getMinutes())})`;

    expect(formatLastWifiAttempt(attempt({ result: "failed", timestamp }), i18n.t.bind(i18n))).toBe(expected);
  });
});
