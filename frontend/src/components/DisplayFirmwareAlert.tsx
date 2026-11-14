import { useTranslation } from "react-i18next";

import { Alert, AlertDescription } from "@/components/ui/alert";

export function DisplayFirmwareAlert({ firmware }: { firmware?: string | null }) {
  const { t } = useTranslation();
  return firmware === "1" ? <Alert><AlertDescription>{t("display.firmware.legacy")}</AlertDescription></Alert> : null;
}
