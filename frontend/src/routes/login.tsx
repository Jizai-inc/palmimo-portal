import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { AuthShell } from "@/components/AuthShell";
import { LoginPanel } from "@/components/LoginPanel";

/**
 * Password login -- reached whenever the guard's session probe
 * (src/lib/authGate.ts) finds no valid session. Route + `AuthShell` chrome
 * only; logic lives in `LoginPanel`, which takes callbacks instead of
 * calling `useNavigate` itself so it's unit-testable -- see
 * components/LoginPanel.test.tsx.
 */
export const Route = createFileRoute("/login")({
  component: LoginScreen,
});

function LoginScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const isSticker = status?.auth_state === "initial";

  return (
    <AuthShell
      title={isSticker ? t("login.stickerTitle") : t("login.title")}
      description={isSticker ? t("login.stickerBody") : undefined}
    >
      <LoginPanel
        onLoggedIn={(mode, connected) => {
          if (mode === "initial") {
            void navigate({ to: "/change-password" });
            return;
          }
          void navigate({ to: connected ? "/dashboard" : "/wifi" });
        }}
        onForgotPassword={() => void navigate({ to: "/reset-login" })}
      />
    </AuthShell>
  );
}
