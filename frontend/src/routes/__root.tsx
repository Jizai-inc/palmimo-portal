import { Outlet, createRootRoute, redirect } from "@tanstack/react-router";

import { GATE_PATHS, isPathAllowedForGate, resolveAuthGateSafely } from "@/lib/authGate";

/**
 * Route guard: on every navigation, resolves which screen the current
 * `system/status` (plus a session probe -- src/lib/authGate.ts) requires,
 * and redirects there unless the current path is allowed under that gate.
 *
 * This is the frontend half of the double-gate: the server enforces access,
 * the frontend only shapes the UX -- a bug here is a UX regression, not a
 * security hole, since every endpoint also enforces its own access rule
 * server-side.
 *
 * "Allowed" is not just an exact match against `GATE_PATHS[gate.screen]`:
 * see {@link isPathAllowedForGate} for its two carve-outs (dashboard-family
 * sub-pages, `/wifi/waiting`'s AP-teardown exception).
 *
 * Probes go through {@link resolveAuthGateSafely}, never the raw
 * `resolveAuthGate`, so a probe failure resolves to `/status-error` instead
 * of throwing into TanStack Router's default error boundary -- see
 * routes/status-error.tsx and main.tsx's `defaultErrorComponent` backstop.
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
