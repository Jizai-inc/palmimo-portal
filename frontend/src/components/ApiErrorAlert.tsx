import { useTranslation } from "react-i18next";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { PortalApiError } from "@/api/client";

/**
 * Resolves a {@link PortalApiError}'s machine-readable `code` through `errors.<code>` in the
 * active locale -- the backend returns only the code, never a sentence. `error` is not always a
 * {@link PortalApiError}: a request that never reaches the backend (AP torn down mid-request)
 * surfaces as a plain `TypeError` from `fetch` and gets a generic network-error message instead.
 */
export function ApiErrorAlert({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) {
    return null;
  }
  const message = error instanceof PortalApiError
    ? t(`errors.${error.code}`, { ...error.params, defaultValue: t("errors.http_error") })
    : t("common.networkError");
  return (
    <Alert variant="destructive">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}
