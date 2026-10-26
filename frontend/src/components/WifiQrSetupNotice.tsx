import { useTranslation } from "react-i18next";

import type { WifiSetupInfo } from "@/api/generated/models";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

export function WifiQrSetupNotice({ setup }: { setup: WifiSetupInfo | null | undefined }) {
  const { t } = useTranslation();
  if (setup?.mode === "unavailable" && setup.reason === "firmware_too_old") {
    return (
      <Alert>
        <AlertTitle>{t("wifiQr.oldFirmwareTitle")}</AlertTitle>
        <AlertDescription className="flex flex-col items-start gap-3">
          <p>{t("wifiQr.oldFirmwareBody")}</p>
          <Button variant="outline" asChild>
            <a href="/settings/update#face-firmware">{t("update.displayFirmwareRepair")}</a>
          </Button>
        </AlertDescription>
      </Alert>
    );
  }
  if (!setup || !["starting", "active"].includes(setup.mode)) return null;

  return (
    <Alert>
      <AlertTitle>{t("wifiQr.title")}</AlertTitle>
      <AlertDescription>{t("wifiQr.body")}</AlertDescription>
    </Alert>
  );
}
