import { Eye, EyeOff } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { WifiAttemptInfo, WifiNetworkResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * The password-entry step of `/wifi`'s scan -> pick -> enter password -> submit flow. Split
 * out of `routes/wifi.tsx` so it can be unit-tested without a router context.
 */
export function WifiConnectForm({
  network,
  lastAttempt,
  connectError,
  isSubmitting,
  onSubmit,
  onBack,
}: {
  network: WifiNetworkResponse;
  lastAttempt: WifiAttemptInfo | null | undefined;
  connectError: unknown;
  isSubmitting: boolean;
  onSubmit: (psk: string) => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const [psk, setPsk] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const lastAttemptFailed = lastAttempt?.result === "failed";
  // Mirrors the server's WPA2 passphrase length rule (api/wifi.py's _validate_psk).
  const pskTooShort = network.secured && psk.length > 0 && psk.length < 8;

  // Defends against `network` changing under a live instance (routes/wifi.tsx currently
  // unmounts instead, but that's an implementation detail this component shouldn't rely on).
  useEffect(() => {
    setPsk("");
    setShowPassword(false);
  }, [network.ssid]);

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (pskTooShort) return;
    onSubmit(psk);
  }

  return (
    <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
      {lastAttemptFailed && lastAttempt ? (
        <Alert variant="destructive">
          <AlertTitle>{t("wifi.lastAttemptFailedTitle")}</AlertTitle>
          <AlertDescription>{t("wifi.lastAttemptFailed", { ssid: lastAttempt.ssid })}</AlertDescription>
        </Alert>
      ) : null}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="psk">{t("wifi.passwordLabel")}</Label>
        <div className="relative">
          <Input
            id="psk"
            type={showPassword ? "text" : "password"}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            required={network.secured}
            value={psk}
            onChange={(event) => setPsk(event.target.value)}
            className="pr-10"
          />
          <button
            type="button"
            onClick={() => setShowPassword((shown) => !shown)}
            aria-pressed={showPassword}
            aria-label={t("wifi.togglePasswordVisibility")}
            className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-muted-foreground hover:text-foreground"
          >
            {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
          </button>
        </div>
        {pskTooShort ? <p className="text-sm text-destructive">{t("wifi.passwordTooShort")}</p> : null}
      </div>
      <ApiErrorAlert error={connectError} />
      <div className="flex gap-3">
        <Button type="button" variant="outline" className="flex-1" onClick={onBack}>
          {t("common.back")}
        </Button>
        <Button type="submit" disabled={isSubmitting || pskTooShort} className="flex-1">
          {t("wifi.connect")}
        </Button>
      </div>
    </form>
  );
}
