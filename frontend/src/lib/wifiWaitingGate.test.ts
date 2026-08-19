import { describe, expect, it } from "vitest";

import { shouldRedirectToWifiScan } from "./wifiWaitingGate";

describe("shouldRedirectToWifiScan", () => {
  it("is true when ssid is empty -- a direct/stale visit with no connect attempt behind it", () => {
    expect(shouldRedirectToWifiScan({ ssid: "" })).toBe(true);
  });

  it("is false once ssid is present -- the normal case, reached from /wifi's own submit", () => {
    expect(shouldRedirectToWifiScan({ ssid: "Home-5G" })).toBe(false);
  });
});
