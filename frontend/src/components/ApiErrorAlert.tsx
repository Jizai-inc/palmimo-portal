import { useTranslation } from "react-i18next";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { PortalApiError } from "@/api/client";
import { useGetUpdateJobApiV1PlatformUpdateGet } from "@/api/generated/platform/platform";
import { apiErrorMessage } from "@/lib/apiErrorMessage";

/** Destructive alert for a failed request; see {@link apiErrorMessage}. */
export function ApiErrorAlert({ error }: { error: unknown }) {
  const { t } = useTranslation();
  const appConflict = error instanceof PortalApiError && ["app_job_in_progress", "app_busy"].includes(error.code);
  const { data: platformJob } = useGetUpdateJobApiV1PlatformUpdateGet({ query: { enabled: appConflict } });
  const platformUpdating = appConflict && platformJob?.job.state === "running";
  if (!error) {
    return null;
  }
  const message = platformUpdating ? t("update.platformUpdating") : apiErrorMessage(t, error);
  return (
    <Alert variant="destructive">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}
