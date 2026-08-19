import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useLoginApiV1AuthLoginPost } from "@/api/generated/auth/auth";
import { getGetStatusApiV1SystemStatusGetQueryKey, useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * routes/login.tsx's logic, router-free like the other `*Panel` components:
 * `onLoggedIn`/`onForgotPassword` are the only reach-out to routing. Renders
 * one of two copy variants depending on `auth_state`: the device's sticker
 * password ("initial") or a normal login ("set") -- see
 * palmimo-portal-technical.md's cross-cutting decision 1.
 *
 * The "Forgot your password?" link is shown only on the normal ("set")
 * variant and only when `device_id` is present in `system/status` -- never
 * for the sticker variant, and never on a DIY device, since `POST
 * /auth/reset` refuses those server-side (core/auth.py's `decide_reset`).
 */
export function LoginPanel({
  onLoggedIn,
  onForgotPassword,
}: {
  onLoggedIn: (mode: "initial" | "full", connected: boolean) => void;
  onForgotPassword: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const [password, setPassword] = useState("");
  const mutation = useLoginApiV1AuthLoginPost();

  const isSticker = status?.auth_state === "initial";
  const showForgotPassword = !isSticker && Boolean(status?.device_id);

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    mutation.mutate(
      { data: { password } },
      {
        onSuccess: (response) => {
          // The screen this navigates to (dashboard, or the forced
          // change-password step) must not render the pre-login
          // auth_state from cache.
          void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
          const mode = response.mode === "initial" ? "initial" : "full";
          onLoggedIn(mode, status?.state === "connected");
        },
      },
    );
  }

  return (
    <>
      {isSticker && status?.device_id ? (
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t("login.deviceIdLabel")}</span>
          <Badge variant="outline">{status.device_id}</Badge>
        </div>
      ) : null}
      <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="password">{t("login.passwordLabel")}</Label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <ApiErrorAlert error={mutation.error} />
        <Button type="submit" className="w-full" disabled={mutation.isPending}>
          {t("login.submit")}
        </Button>
        {showForgotPassword ? (
          <button
            type="button"
            onClick={onForgotPassword}
            className="text-center text-sm text-muted-foreground underline underline-offset-2"
          >
            {t("login.forgotPassword")}
          </button>
        ) : null}
      </form>
      {isSticker ? <p className="text-xs text-muted-foreground">{t("login.hint")}</p> : null}
    </>
  );
}
