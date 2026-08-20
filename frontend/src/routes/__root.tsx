import { Outlet, createRootRoute, redirect } from "@tanstack/react-router";

import { GATE_PATHS, isPathAllowedForGate, resolveAuthGateSafely } from "@/lib/authGate";

/**
 * Route guard: on every navigation, resolves which screen the current `system/status` plus a
 * session probe (src/lib/authGate.ts) requires, and redirects there unless the current path is
 * allowed under that gate. This is the frontend half of the double-gate: the server enforces
 * access, the frontend only shapes the UX -- a bug here is a UX regression, not a security hole.
 * "Allowed" has two carve-outs; see {@link isPathAllowedForGate}. Uses
 * {@link resolveAuthGateSafely}, not the raw `resolveAuthGate`, so a probe failure resolves to
 * `/status-error` instead of throwing into the router's default error boundary.
 */
export const Route = createRootRoute({
  beforeLoad: async ({ location }) => {
    const gate = await resolveAuthGateSafely();
    const search = gate.screen === "status-error" ? { reason: gate.reason } : undefined;
    if (!isPathAllowedForGate(gate, location.pathname)) {
      throw redirect({ to: GATE_PATHS[gate.screen], search });
    }
  },
  component: () => (
    <div className="min-h-screen bg-background text-foreground">
      <Outlet />
    </div>
  ),
});
