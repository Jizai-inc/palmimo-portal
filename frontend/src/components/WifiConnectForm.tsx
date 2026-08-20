import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { WifiAttemptInfo, WifiNetworkResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * The password-entry step of `/wifi`'s scan -> pick -> enter password ->
 * submit flow. Split out of `routes/wifi.tsx` so it can be unit-tested
 * without a router context -- it takes the selected network and the
 * previous failed attempt (if any) as plain props and calls `onSubmit`
 * with the entered password; it does not know about `useNavigate` or
 * `Route.useSearch()`.
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
  const lastAttemptFailed = lastAttempt?.result === "failed";
  // Mirrors the server's WPA2 passphrase length rule (api/wifi.py's
  // _validate_psk): 1..7 characters is never valid for a secured network.
  // ssid comes from the scan list, so it never needs a client-side check --
  // only the server can see the raw bytes a real device would send.
  const pskTooShort = network.secured && psk.length > 0 && psk.length < 8;

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
        <Input
          id="psk"
          type="password"
          autoComplete="off"
          required={network.secured}
          value={psk}
          onChange={(event) => setPsk(event.target.value)}
        />
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
