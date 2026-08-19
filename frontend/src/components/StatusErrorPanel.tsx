import { CircleX, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { CenteredState } from "@/components/CenteredState";
import { Button } from "@/components/ui/button";

/**
 * routes/status-error.tsx's logic, router-free like the other `*Panel`
 * components: `onRetry`/`onReset` are the only reach-out to routing.
 *
 * - **`"unavailable"`** -- transient, worth a retry: shows `onRetry`'s
 *   button, disabled while in flight.
 * - **`"corrupt"`** -- permanent from the browser's side, but only on an
 *   identity-carrying device is there anything to do about it: the reset
 *   button renders only once `system/status` reports a `device_id` (a UX
 *   affordance -- `core/auth.py`'s `decide_reset` refuses reset server-side
 *   on a DIY device regardless). A DIY device with a corrupt `auth.json`
 *   has no `device_id` and sees `errors.auth_state_corrupt` instead, since
 *   recovery there is SSH-only.
 */
export function StatusErrorPanel({
  reason,
  onRetry,
  onReset,
}: {
  reason: "unavailable" | "corrupt";
  onRetry: () => Promise<void>;
  onReset: () => void;
}) {
  const { t } = useTranslation();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const [isRetrying, setIsRetrying] = useState(false);
  const isCorrupt = reason === "corrupt";
  const hasIdentity = Boolean(status?.device_id);

  async function handleRetry() {
    setIsRetrying(true);
    try {
      await onRetry();
    } finally {
      setIsRetrying(false);
    }
  }

  return (
    <CenteredState
      icon={isCorrupt ? CircleX : TriangleAlert}
      body={
        isCorrupt
          ? hasIdentity
            ? t("status.corrupt.bodyWithReset")
            : t("errors.auth_state_corrupt")
          : t("status.unavailable.body")
      }
      action={
        isCorrupt ? (
          hasIdentity ? (
            <Button variant="destructive" onClick={onReset}>
              {t("resetLogin.resetButton")}
            </Button>
          ) : null
        ) : (
          <Button variant="outline" onClick={() => void handleRetry()} disabled={isRetrying}>
            {t("common.retry")}
          </Button>
        )
      }
    />
  );
}
