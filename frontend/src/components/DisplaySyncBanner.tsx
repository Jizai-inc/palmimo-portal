import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useSyncDisplayAssetsApiV1DisplayAssetsSyncPost } from "@/api/generated/display-assets/display-assets";
import type { DisplaySyncInfo } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

export function DisplaySyncBanner({ status }: { status?: DisplaySyncInfo }) {
  const { t } = useTranslation();
  const client = useQueryClient();
  const sync = useSyncDisplayAssetsApiV1DisplayAssetsSyncPost({ mutation: {
    onSettled: () => client.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() }),
  } });
  useEffect(() => {
    if (!sync.isPending && (!status?.pending || status?.running)) sync.reset();
  }, [status?.pending, status?.running, sync.isPending, sync.reset]);
  const running = status?.running || sync.isPending;
  if (!status?.pending && !running) return null;
  return <div className="flex flex-col gap-2">
    <Alert role="status">
      <AlertDescription className="flex flex-wrap items-center gap-3">
        {running ? <Loader2 className="size-4 animate-spin" aria-hidden /> : null}
        <span className="flex-1">{running ? t("display.firmware.syncRunning") : sync.data?.result === "busy" ? t("display.firmware.syncBusy") : t("display.firmware.syncPending")}</span>
        {status?.pending ? <Button size="sm" variant="outline" disabled={running} onClick={() => sync.mutate()}>{t("display.firmware.syncNow")}</Button> : null}
      </AlertDescription>
    </Alert>
    <ApiErrorAlert error={sync.error} />
    {sync.data?.result === "failed" && !running ? <Alert variant="destructive"><AlertDescription>{t("errors.display_sync_failed")}</AlertDescription></Alert> : null}
  </div>;
}
