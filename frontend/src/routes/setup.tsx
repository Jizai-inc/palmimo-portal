import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useSetupApiV1AuthSetupPost } from "@/api/generated/auth/auth";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AuthShell } from "@/components/AuthShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/** Open first-time setup -- reachable only when `auth_state === "open_setup"` (a DIY, self-flashed image). */
export const Route = createFileRoute("/setup")({
  component: SetupScreen,
});

function SetupScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [mismatch, setMismatch] = useState(false);
  const mutation = useSetupApiV1AuthSetupPost();

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== confirmPassword) {
      setMismatch(true);
      return;
    }
    setMismatch(false);
    mutation.mutate(
      { data: { password } },
      { onSuccess: () => void navigate({ to: "/login" }) },
    );
  }

  return (
    <AuthShell title={t("setup.title")} description={t("setup.body")}>
      <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="password">{t("setup.passwordLabel")}</Label>
          <Input
            id="password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="confirm-password">{t("setup.confirmPasswordLabel")}</Label>
          <Input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            required
            value={confirmPassword}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
        </div>
        {mismatch ? <p className="text-sm text-destructive">{t("setup.passwordMismatch")}</p> : null}
        <ApiErrorAlert error={mutation.error} />
        <Button type="submit" className="w-full" disabled={mutation.isPending}>
          {t("setup.submit")}
        </Button>
      </form>
    </AuthShell>
  );
}
