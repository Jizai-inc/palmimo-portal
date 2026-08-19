import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AuthShell } from "@/components/AuthShell";
import { ResetLoginPanel } from "@/components/ResetLoginPanel";

/**
 * Login-credentials-reset screen: route + `AuthShell` chrome only. Logic
 * lives in `ResetLoginPanel`, which takes `onBack`/`onDone` instead of
 * calling `useNavigate` itself so it's unit-testable -- see
 * components/ResetLoginPanel.test.tsx.
 *
 * Reached from login.tsx's "Forgot your password?" link and
 * status-error.tsx's reset button -- see `isPathAllowedForGate`
 * (src/lib/authGate.ts) for which gates allow this path.
 */
export const Route = createFileRoute("/reset-login")({
  component: ResetLoginScreen,
});

function ResetLoginScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <AuthShell title={t("resetLogin.title")} description={t("resetLogin.body")}>
      <ResetLoginPanel
        onBack={() => void navigate({ to: "/login" })}
        onDone={() => void navigate({ to: "/login" })}
      />
    </AuthShell>
  );
}
