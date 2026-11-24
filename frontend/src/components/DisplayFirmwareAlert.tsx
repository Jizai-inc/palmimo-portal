import { useTranslation } from "react-i18next";

import { useGetPlatformApiV1PlatformGet } from "@/api/generated/platform/platform";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

export function DisplayFirmwareAlert({ firmware }: { firmware?: string | null }) {
  const { t } = useTranslation();
  const requiresFirmware = firmware === "1" || firmware === "2";
  const { data: platform, error } = useGetPlatformApiV1PlatformGet(undefined, {
    query: { enabled: requiresFirmware },
  });
  if (!requiresFirmware) return null;
  const desired = error ? null : platform?.display_firmware_desired;
  if (firmware === "2" && desired === 2) return null;
  return (
    <div className="flex flex-col items-start gap-2">
      {firmware === "1" ? <Badge role="note" variant="outline">{t("display.firmware.legacy")}</Badge> : null}
      {desired == null ? (
        <p role="status" className="text-xs text-muted-foreground">{t("display.firmware.switchUnknown")}</p>
      ) : Number(firmware) !== desired ? (
        <Alert><AlertDescription>{t("display.firmware.switchRequired")}</AlertDescription></Alert>
      ) : null}
    </div>
  );
}
