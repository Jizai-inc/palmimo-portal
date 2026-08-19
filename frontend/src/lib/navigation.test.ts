import { describe, expect, it } from "vitest";

import { isPathAllowedForGate } from "@/lib/authGate";
import { NAV_ITEMS, isActive } from "@/lib/navigation";

describe("NAV_ITEMS", () => {
  it("includes the dashboard, wifi-settings, ssh-keys, power, and update routes, in that order", () => {
    expect(NAV_ITEMS.map((item) => item.to)).toEqual(["/dashboard", "/wifi-settings", "/ssh-keys", "/power", "/update"]);
  });

  // Checks reachability under the dashboard gate directly, not via a raw
  // list-equality check against DASHBOARD_FAMILY_PATHS.
  it.each(NAV_ITEMS)("$to is allowed under the dashboard gate", (item) => {
    expect(isPathAllowedForGate({ screen: "dashboard" }, item.to)).toBe(true);
  });

  it.each(NAV_ITEMS)("$to is NOT allowed under the login gate", (item) => {
    expect(isPathAllowedForGate({ screen: "login", variant: "normal", hasIdentity: true }, item.to)).toBe(false);
  });

  // The Wi-Fi settings "connect to another network" flow reuses the setup
  // scan/waiting screens, so both are reachable under the dashboard gate
  // (see authGate.ts's isPathAllowedForGate).
  it("allows /wifi and /wifi/waiting under the dashboard gate", () => {
    expect(isPathAllowedForGate({ screen: "dashboard" }, "/wifi")).toBe(true);
    expect(isPathAllowedForGate({ screen: "dashboard" }, "/wifi/waiting")).toBe(true);
  });
});

describe("isActive", () => {
  it.each([
    ["/dashboard", "/dashboard", true],
    ["/ssh-keys", "/ssh-keys", true],
    ["/dashboard", "/ssh-keys", false],
    ["/ssh-keys", "/power", false],
  ] as const)("isActive(%s, %s) -> %s", (pathname, to, expected) => {
    expect(isActive(pathname, to)).toBe(expected);
  });
});
