import { describe, expect, it } from "vitest";

import { shouldRedirectToWifiScan } from "./wifiWaitingGate";

describe("shouldRedirectToWifiScan", () => {
  it("is true when ssid is empty -- a direct/stale visit with no connect attempt behind it", () => {
    expect(shouldRedirectToWifiScan({ ssid: "", submitted: 1_700_000_000_000 })).toBe(true);
  });

  it("is true when submitted is 0 even with a real-looking ssid -- a hand-typed/shared link (issue #13 review)", () => {
    expect(shouldRedirectToWifiScan({ ssid: "Home-5G", submitted: 0 })).toBe(true);
  });

  it("is false once both ssid and submitted are present -- the normal case, reached from /wifi's own submit", () => {
    expect(shouldRedirectToWifiScan({ ssid: "Home-5G", submitted: 1_700_000_000_000 })).toBe(false);
  });
});
