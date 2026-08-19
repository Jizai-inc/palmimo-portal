import { PortalApiError } from "@/api/client";
import { getStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { getGetStatusApiV1SystemStatusGetQueryOptions } from "@/api/generated/system/system";
import type { SystemStatus } from "@/api/generated/models";
import { queryClient } from "@/lib/queryClient";
import { NAV_ITEMS } from "@/lib/navigation";

/**
 * The screen the route guard (routes/__root.tsx's `beforeLoad`) sends the
 * browser to, derived from `GET /api/v1/system/status` plus a lightweight
 * session probe. See palmimo-portal-technical.md's P1 API spec (auth_state table).
 */
export type AuthGate =
  | { screen: "status-error"; reason: "unavailable" | "corrupt"; hasIdentity: boolean }
  | { screen: "setup" }
  | { screen: "login"; variant: "sticker" | "normal"; hasIdentity: boolean }
  | { screen: "change-password" }
  | { screen: "wifi" }
  | { screen: "dashboard" };

export const GATE_PATHS: Record<AuthGate["screen"], string> = {
  "status-error": "/status-error",
  setup: "/setup",
  login: "/login",
  "change-password": "/change-password",
  wifi: "/wifi",
  dashboard: "/dashboard",
};

/**
 * Every authenticated route reachable once the gate has resolved to
 * `"dashboard"` -- the dashboard itself plus its sub-pages (SSH keys, power
 * controls). Derived from `NAV_ITEMS` (lib/navigation.ts) so a route added
 * there is automatically reachable here too, without the two drifting apart.
 */
export const DASHBOARD_FAMILY_PATHS: readonly string[] = NAV_ITEMS.map((item) => item.to);

/**
 * Whether `pathname` is reachable while the guard (routes/__root.tsx's
 * `beforeLoad`) has resolved to `gate`, beyond the canonical
 * `GATE_PATHS[gate.screen]` target. Carve-outs, all deliberately narrow:
 *
 * - `"dashboard"` also allows every {@link DASHBOARD_FAMILY_PATHS} entry
 *   (same full-session-plus-connected-Wi-Fi precondition as `/dashboard`),
 *   plus `/wifi` and `/wifi/waiting`, which the Wi-Fi settings screen's
 *   "connect to another network" reconfigure flow reuses.
 * - `"wifi"` also allows `/wifi/waiting`, which the connect form navigates
 *   to (routes/wifi.tsx) and which must not be bounced away on arrival.
 * - `"status-error"` with `reason === "unavailable"` also allows
 *   `/wifi/waiting`: connecting tears the AP down and `system/status` can
 *   transiently fail in that window (see palmimo-portal-technical.md's
 *   AP-disconnection-asymmetry section), so the waiting screen's own polling
 *   must be allowed to run. Not granted for `"corrupt"`, a durable problem.
 * - `/reset-login` is reachable only from `"login"` with `variant ===
 *   "normal"` or `"status-error"` with `reason === "corrupt"`, and only when
 *   `gate.hasIdentity` is true (see {@link resolveAuthGate}). Under the
 *   sticker variant there is nothing to reset (`POST /auth/reset` answers
 *   409 `auth_not_set`); under `"setup"` or without identity the server
 *   refuses with 403 `reset_not_available` (`core/auth.py`'s `decide_reset`),
 *   so the link would be a dead end.
 */
export function isPathAllowedForGate(gate: AuthGate, pathname: string): boolean {
  if (pathname === GATE_PATHS[gate.screen]) {
    return true;
  }
  if (gate.screen === "dashboard" && (DASHBOARD_FAMILY_PATHS as string[]).includes(pathname)) {
    return true;
  }
  if (gate.screen === "dashboard" && (pathname === "/wifi" || pathname === "/wifi/waiting")) {
    return true;
  }
  if (gate.screen === "wifi" && pathname === "/wifi/waiting") {
    return true;
  }
  if (gate.screen === "status-error" && gate.reason === "unavailable" && pathname === "/wifi/waiting") {
    return true;
  }
  if (pathname === "/reset-login") {
    if (gate.screen === "login" && gate.variant === "normal" && gate.hasIdentity) {
      return true;
    }
    if (gate.screen === "status-error" && gate.reason === "corrupt" && gate.hasIdentity) {
      return true;
    }
  }
  return false;
}

/**
 * Probe whether the browser already holds a valid session, using
 * `GET /api/v1/wifi/status` -- gated by `require_wifi_access` +
 * `require_full_session` (palmimo_portal/deps.py) on every device once
 * `auth_state` has left `open_setup`, so its outcome doubles as a session
 * check without a dedicated "whoami" endpoint:
 *
 * - 200 -> a full session is present.
 * - 403 `initial_password_must_be_changed` -> a valid *initial*-mode
 *   session (logged in with the sticker password, not yet changed).
 * - 401 `not_authenticated` -> no valid session at all.
 *
 * Lets a reload survive login without the guard remembering anything client-side.
 */
async function probeSession(): Promise<"full" | "initial" | "none"> {
  try {
    await getStatusApiV1WifiStatusGet();
    return "full";
  } catch (error) {
    if (error instanceof PortalApiError) {
      if (error.code === "initial_password_must_be_changed") {
        return "initial";
      }
      if (error.code === "not_authenticated") {
        return "none";
      }
    }
    throw error;
  }
}

/** Resolve the current system status into the screen the guard should route to. */
export async function resolveAuthGate(status: SystemStatus): Promise<AuthGate> {
  const hasIdentity = status.device_id != null;
  if (status.auth_state === "unavailable") {
    return { screen: "status-error", reason: "unavailable", hasIdentity };
  }
  if (status.auth_state === "corrupt") {
    return { screen: "status-error", reason: "corrupt", hasIdentity };
  }
  if (status.auth_state === "open_setup") {
    return { screen: "setup" };
  }

  const session = await probeSession();
  if (session === "none") {
    return { screen: "login", variant: status.auth_state === "initial" ? "sticker" : "normal", hasIdentity };
  }
  if (session === "initial") {
    return { screen: "change-password" };
  }
  return status.state === "connected" ? { screen: "dashboard" } : { screen: "wifi" };
}

/**
 * The guard's entry point (routes/__root.tsx's `beforeLoad`): fetches
 * `system/status` and resolves the gate, never letting a failure of either
 * probe escape as a thrown error. Otherwise a failed `system/status` fetch
 * (AP torn down mid-navigation, device still booting) would reject
 * `beforeLoad` and land on TanStack Router's default error boundary (an
 * unstyled, non-i18n'd stack trace) instead of a screen this app owns.
 */
export async function resolveAuthGateSafely(): Promise<AuthGate> {
  try {
    const status = await queryClient.fetchQuery(getGetStatusApiV1SystemStatusGetQueryOptions());
    return await resolveAuthGate(status);
  } catch {
    return { screen: "status-error", reason: "unavailable", hasIdentity: false };
  }
}
