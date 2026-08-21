import { Outlet, createRootRoute } from "@tanstack/react-router";

import { runAuthGate } from "@/lib/authGate";

/**
 * Route guard: on every navigation, resolves which screen the current `system/status` plus a
 * session probe requires, and redirects there unless the current path is allowed under that
 * gate -- except `/wifi/waiting`, which skips the probe entirely (see
 * {@link runAuthGate}/`shouldSkipAuthGate` in `lib/authGate.ts`, the actual decision logic).
 * This is the frontend half of the double-gate: the server enforces access, the frontend only
 * shapes the UX -- a bug here is a UX regression, not a security hole.
 */
export const Route = createRootRoute({
  beforeLoad: ({ location }) => runAuthGate(location.pathname),
  component: () => (
    <div className="min-h-screen bg-background text-foreground">
      <Outlet />
    </div>
  ),
});
