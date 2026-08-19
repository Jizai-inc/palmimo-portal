import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useChangePasswordEndpointApiV1AuthChangePasswordPost } from "@/api/generated/auth/auth";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AuthShell } from "@/components/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Forced change-password step for an initial-mode session (sticker password
 * not yet replaced). Unskippable: this and `POST /auth/logout` are the only
 * authenticated endpoints reachable in that mode server-side (see
 * `require_full_session` in palmimo_portal/deps.py).
 */
export const Route = createFileRoute("/change-password")({
  component: ChangePasswordScreen,
});

function ChangePasswordScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const mutation = useChangePasswordEndpointApiV1AuthChangePasswordPost();

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (newPassword !== confirmPassword) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    mutation.mutate(
      { data: { current_password: currentPassword, new_password: newPassword } },
      {
        onSuccess: () => {
          // No screen reachable after this point may keep rendering the
          // pre-change auth state (auth_state === "initial") from cache.
          queryClient.clear();
          void navigate({ to: status?.state === "connected" ? "/dashboard" : "/wifi" });
        },
      },
    );
  }

  return (
    <AuthShell title={t("changePassword.title")} description={t("changePassword.body")}>
      <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="current-password">{t("changePassword.currentPasswordLabel")}</Label>
          <Input
            id="current-password"
            type="password"
            autoComplete="current-password"
            required
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="new-password">{t("changePassword.newPasswordLabel")}</Label>
          <Input
            id="new-password"
            type="password"
            autoComplete="new-password"
            placeholder={t("changePassword.newPasswordPlaceholder")}
            required
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="confirm-new-password">{t("changePassword.confirmPasswordLabel")}</Label>
          <Input
            id="confirm-new-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
        </div>
        {mismatch ? <p className="text-sm text-destructive">{t("changePassword.passwordMismatch")}</p> : null}
        <ApiErrorAlert error={mutation.error} />
        <Button type="submit" className="w-full" disabled={mutation.isPending}>
          {t("changePassword.submit")}
        </Button>
      </form>
    </AuthShell>
  );
}
