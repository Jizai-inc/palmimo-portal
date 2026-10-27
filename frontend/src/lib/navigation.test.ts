import { describe, expect, it } from "vitest";

import { isPathAllowedForGate } from "@/lib/authGate";
import { NAV_ITEMS, SETTINGS_ITEMS, isActive } from "@/lib/navigation";

describe("NAV_ITEMS", () => {
  it("lists Home, Apps and Settings, in that order", () => {
    expect(NAV_ITEMS.map((item) => item.to)).toEqual(["/home", "/apps", "/settings"]);
  });

  it.each(NAV_ITEMS)("$to is allowed under the dashboard gate", (item) => {
    expect(isPathAllowedForGate({ screen: "dashboard" }, item.to)).toBe(true);
  });

  it.each(NAV_ITEMS)("$to is NOT allowed under the login gate", (item) => {
    expect(isPathAllowedForGate({ screen: "login", variant: "normal", hasIdentity: true }, item.to)).toBe(false);
  });

  // The Network section's "connect to another network" flow reuses the setup scan/waiting screens.
  it("allows /wifi and /wifi/waiting under the dashboard gate", () => {
    expect(isPathAllowedForGate({ screen: "dashboard" }, "/wifi")).toBe(true);
    expect(isPathAllowedForGate({ screen: "dashboard" }, "/wifi/waiting")).toBe(true);
  });
});

describe("SETTINGS_ITEMS", () => {
  it("lists Network, Secrets, Software update, Device and Developer, in that order", () => {
    expect(SETTINGS_ITEMS.map((item) => item.to)).toEqual([
      "/settings/network",
      "/settings/secrets",
      "/settings/update",
      "/settings/device",
      "/settings/developer",
    ]);
  });

  it.each(SETTINGS_ITEMS)("$to is allowed under the dashboard gate", (item) => {
    expect(isPathAllowedForGate({ screen: "dashboard" }, item.to)).toBe(true);
  });
});

describe("isActive", () => {
  it.each([
    ["/home", "/home", true],
    ["/settings/device", "/settings", true],
    ["/apps/add", "/apps", true],
    ["/home", "/settings", false],
    ["/settings-old", "/settings", false],
    ["/settings/device", "/settings/developer", false],
  ] as const)("isActive(%s, %s) -> %s", (pathname, to, expected) => {
    expect(isActive(pathname, to)).toBe(expected);
  });
});
