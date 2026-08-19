import { createFileRoute, useNavigate, useRouter } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AuthShell } from "@/components/AuthShell";
import { StatusErrorPanel } from "@/components/StatusErrorPanel";

/**
 * Covers `auth_state` `"unavailable"` / `"corrupt"` (src/lib/authGate.ts's
 * `AuthGate`) and the guard's own failure when `system/status` itself throws
 * (see `resolveAuthGateSafely`). `reason`, set by the guard on redirect,
 * picks between two distinct copies -- see `StatusErrorPanel` for the
 * body/action logic, kept router-free so it can be unit-tested directly.
 */
interface StatusErrorSearch {
  reason: "unavailable" | "corrupt";
}

export const Route = createFileRoute("/status-error")({
  validateSearch: (search: Record<string, unknown>): StatusErrorSearch => ({
    reason: search.reason === "corrupt" ? "corrupt" : "unavailable",
  }),
  component: StatusErrorScreen,
});

function StatusErrorScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const router = useRouter();
  const { reason } = Route.useSearch();
  const isCorrupt = reason === "corrupt";

  async function handleRetry() {
    // Re-runs the whole guard (routes/__root.tsx's `beforeLoad`), not just
    // this screen's data, so it lands back here only if still broken.
    await router.invalidate();
    await navigate({ to: "/" });
  }

  return (
    <AuthShell title={isCorrupt ? t("status.corrupt.title") : t("status.unavailable.title")}>
      <StatusErrorPanel
        reason={reason}
        onRetry={handleRetry}
        onReset={() => void navigate({ to: "/reset-login" })}
      />
    </AuthShell>
  );
}
