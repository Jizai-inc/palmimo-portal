import { PortalApiError } from "@/api/client";
import { getStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { getGetStatusApiV1SystemStatusGetQueryOptions } from "@/api/generated/system/system";
import type { SystemStatus } from "@/api/generated/models";
import { queryClient } from "@/lib/queryClient";
import { NAV_ITEMS } from "@/lib/navigation";

/**
 * The screen the route guard (routes/__root.tsx's `beforeLoad`) sends the browser to, derived
 * from `GET /api/v1/system/status` plus a lightweight session probe.
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
 * Every authenticated route reachable once the gate has resolved to `"dashboard"`. Derived
 * from `NAV_ITEMS` (lib/navigation.ts) so a route added there is automatically reachable here.
 */
export const DASHBOARD_FAMILY_PATHS: readonly string[] = NAV_ITEMS.map((item) => item.to);

/**
 * Whether `pathname` is reachable while the guard has resolved to `gate`, beyond the canonical
 * `GATE_PATHS[gate.screen]` target. Narrow carve-outs:
 *
 * - `"dashboard"` also allows every {@link DASHBOARD_FAMILY_PATHS} entry, plus `/wifi` and
 *   `/wifi/waiting` (the reconfigure flow reuses them).
 * - `"wifi"` also allows `/wifi/waiting`.
 * - `"status-error"` with `reason === "unavailable"` also allows `/wifi/waiting`: connecting
 *   tears the AP down and `system/status` can transiently fail in that window, so the waiting
 *   screen's own polling must keep running. Not granted for `"corrupt"`, a durable problem.
 * - `/reset-login` is reachable only from `"login"` (normal variant) or `"status-error"`
 *   (corrupt), and only with `gate.hasIdentity` -- otherwise the server refuses with 409/403.
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
 * Probe session state via `GET /api/v1/wifi/status`, doubling as a session check without a
 * dedicated "whoami" endpoint: 200 -> full session; 403 `initial_password_must_be_changed` ->
 * valid initial-mode session; 401 `not_authenticated` -> none.
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
 * The guard's entry point: fetches `system/status` and resolves the gate, never letting a
 * failure of either probe escape as a thrown error -- otherwise a failed fetch would reject
 * `beforeLoad` and land on the router's default error boundary instead of a screen this app owns.
 */
export async function resolveAuthGateSafely(): Promise<AuthGate> {
  try {
    const status = await queryClient.fetchQuery(getGetStatusApiV1SystemStatusGetQueryOptions());
    return await resolveAuthGate(status);
  } catch {
    return { screen: "status-error", reason: "unavailable", hasIdentity: false };
  }
}
