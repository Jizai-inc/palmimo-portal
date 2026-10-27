import type { TFunction } from "i18next";

import { PortalApiError } from "@/api/client";

/**
 * Resolves a {@link PortalApiError}'s machine-readable `code` through `errors.<code>` in the
 * active locale -- the backend returns only the code, never a sentence. Any other error is a
 * request that never reached the backend (AP torn down mid-request, a plain `TypeError` from
 * `fetch`) and gets the generic network-error message.
 */
export function apiErrorMessage(t: TFunction, error: unknown): string {
  return error instanceof PortalApiError
    ? t(`errors.${error.code}`, { ...error.params, defaultValue: t("errors.http_error") })
    : t("common.networkError");
}
