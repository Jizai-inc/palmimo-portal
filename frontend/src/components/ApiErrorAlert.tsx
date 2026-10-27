import { useTranslation } from "react-i18next";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { apiErrorMessage } from "@/lib/apiErrorMessage";

/** Destructive alert for a failed request; see {@link apiErrorMessage}. */
export function ApiErrorAlert({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!error) {
    return null;
  }
  const message = apiErrorMessage(t, error);
  return (
    <Alert variant="destructive">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}
