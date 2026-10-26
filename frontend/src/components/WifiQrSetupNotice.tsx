import { useTranslation } from "react-i18next";

import type { WifiSetupInfo } from "@/api/generated/models";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export function WifiQrSetupNotice({ setup }: { setup: WifiSetupInfo | null | undefined }) {
  const { t } = useTranslation();
  if (!setup || !["starting", "active"].includes(setup.mode)) return null;

  return (
    <Alert>
      <AlertTitle>{t("wifiQr.title")}</AlertTitle>
      <AlertDescription>{t("wifiQr.body")}</AlertDescription>
    </Alert>
  );
}
