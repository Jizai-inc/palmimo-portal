import { afterEach, describe, expect, it, vi } from "vitest";
import { http } from "msw";

import { DASHBOARD_FAMILY_PATHS, isPathAllowedForGate, resolveAuthGateSafely, runAuthGate } from "@/lib/authGate";
import type { AuthGate } from "@/lib/authGate";
import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1WifiStatusGetMockHandler } from "@/api/generated/wifi/wifi.msw";
import { queryClient } from "@/lib/queryClient";
import { server } from "@/test/server";

const LOGIN_GATE: AuthGate = { screen: "login", variant: "normal", hasIdentity: true };
const DIY_LOGIN_GATE: AuthGate = { screen: "login", variant: "normal", hasIdentity: false };
const STICKER_LOGIN_GATE: AuthGate = { screen: "login", variant: "sticker", hasIdentity: true };
const WIFI_GATE: AuthGate = { screen: "wifi" };
const SETUP_GATE: AuthGate = { screen: "setup" };
const DASHBOARD_GATE: AuthGate = { screen: "dashboard" };
const STATUS_ERROR_CORRUPT_GATE: AuthGate = { screen: "status-error", reason: "corrupt", hasIdentity: true };
const STATUS_ERROR_CORRUPT_DIY_GATE: AuthGate = { screen: "status-error", reason: "corrupt", hasIdentity: false };
const STATUS_ERROR_UNAVAILABLE_GATE: AuthGate = {
  screen: "status-error",
  reason: "unavailable",
  hasIdentity: true,
};

describe("isPathAllowedForGate", () => {
  it.each([
    // gate, pathname, expected
    [LOGIN_GATE, "/login", true],
    [SETUP_GATE, "/setup", true],
    [WIFI_GATE, "/wifi", true],
    [DASHBOARD_GATE, "/dashboard", true],
    [DASHBOARD_GATE, "/wifi-settings", true],
    [DASHBOARD_GATE, "/ssh-keys", true],
    [DASHBOARD_GATE, "/power", true],
    [WIFI_GATE, "/wifi/waiting", true],
    // /wifi and /wifi/waiting are also reachable under the dashboard gate:
    // the Wi-Fi settings screen's reconfigure flow reuses the setup
    // scan/waiting screens.
    [DASHBOARD_GATE, "/wifi", true],
    [DASHBOARD_GATE, "/wifi/waiting", true],
    // Negative cases -- a dashboard-family path is not reachable under any
    // other gate, and the wifi-waiting exception is scoped to the wifi and
    // dashboard gates only.
    [LOGIN_GATE, "/ssh-keys", false],
    [WIFI_GATE, "/ssh-keys", false],
    [SETUP_GATE, "/ssh-keys", false],
    [LOGIN_GATE, "/power", false],
    [WIFI_GATE, "/power", false],
    [SETUP_GATE, "/power", false],
    [LOGIN_GATE, "/wifi/waiting", false],
    [SETUP_GATE, "/wifi/waiting", false],
    // /wifi/waiting also survives an "unavailable" status-error gate (the
    // AP is mid-teardown while comitup switches over, and system/status
    // itself can transiently fail during exactly that window) -- but not a
    // "corrupt" one, which is a real, durable auth-state problem, not a
    // transient AP-teardown blip.
    [STATUS_ERROR_UNAVAILABLE_GATE, "/wifi/waiting", true],
    [STATUS_ERROR_CORRUPT_GATE, "/wifi/waiting", false],
    [DASHBOARD_GATE, "/login", false],
    [LOGIN_GATE, "/dashboard", false],
    [WIFI_GATE, "/dashboard", false],
    // /reset-login: reachable only from the normal login gate and the
    // corrupt status-error gate, and only when the device carries an
    // identity (hasIdentity) -- never from the sticker login variant,
    // never from setup (DIY), never from a normal-login/corrupt gate on a
    // DIY device (hasIdentity: false), never from any other gate.
    [LOGIN_GATE, "/reset-login", true],
    [STATUS_ERROR_CORRUPT_GATE, "/reset-login", true],
    [DIY_LOGIN_GATE, "/reset-login", false],
    [STATUS_ERROR_CORRUPT_DIY_GATE, "/reset-login", false],
    [STICKER_LOGIN_GATE, "/reset-login", false],
    [STATUS_ERROR_UNAVAILABLE_GATE, "/reset-login", false],
    [SETUP_GATE, "/reset-login", false],
    [WIFI_GATE, "/reset-login", false],
    [DASHBOARD_GATE, "/reset-login", false],
  ] as const)("gate=%o pathname=%s -> %s", (gate, pathname, expected) => {
    expect(isPathAllowedForGate(gate, pathname)).toBe(expected);
  });

  it("agrees with DASHBOARD_FAMILY_PATHS for every dashboard-family path", () => {
    for (const path of DASHBOARD_FAMILY_PATHS) {
      expect(isPathAllowedForGate(DASHBOARD_GATE, path)).toBe(true);
    }
  });
});

describe("resolveAuthGateSafely", () => {
  afterEach(() => {
    queryClient.clear();
    vi.useRealTimers();
  });

  it("resolves to status-error/unavailable instead of hanging forever when system/status never responds", async () => {
    // Regression for issue #13: the Wi-Fi connect form's own navigate to `/wifi/waiting` waits
    // on this exact probe (routes/__root.tsx's `beforeLoad`) -- a same-origin request that
    // hangs (as it does mid-AP-teardown) must not stall that navigation indefinitely.
    server.use(http.get("*/api/v1/system/status", () => new Promise(() => {})));
    vi.useFakeTimers({ shouldAdvanceTime: true });

    const resultPromise = resolveAuthGateSafely();
    await vi.advanceTimersByTimeAsync(10_000);

    await expect(resultPromise).resolves.toEqual({ screen: "status-error", reason: "unavailable", hasIdentity: false });
  });
});

describe("runAuthGate", () => {
  afterEach(() => {
    queryClient.clear();
  });

  it("skips the gate probe entirely for /wifi/waiting, resolving immediately even when system/status never responds", async () => {
    // Pins issue #13's fix: a hanging handler with no fake timers means this test would itself
    // hang, not just go slow, if runAuthGate ever went back to awaiting the probe here.
    server.use(http.get("*/api/v1/system/status", () => new Promise(() => {})));

    await expect(runAuthGate("/wifi/waiting")).resolves.toBeUndefined();
  });

  it("still probes the gate, and still redirects, for other paths", async () => {
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        state: "connecting",
        hostname: "palmimo-1234",
        auth_state: "set",
        device_id: "1234",
        versions: { portal: "0.1.0", sdk: null },
        last_wifi_attempt: null,
        adapters: "fake",
        state_dir: "/tmp",
      }),
      getGetStatusApiV1WifiStatusGetMockHandler(),
    );

    await expect(runAuthGate("/wifi")).resolves.toBeUndefined();
    await expect(runAuthGate("/dashboard")).rejects.toMatchObject({ options: { to: "/wifi" } });
  });
});
