import { useTranslation } from "react-i18next";

import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppJobDialog } from "@/components/AppJobDialog";
import { AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import type { useAppStart } from "@/lib/useAppStart";

export function AppStartDialogs({ start }: { start: ReturnType<typeof useAppStart> }) {
  const { t } = useTranslation();
  return <>
    <AlertDialog open={start.confirmationVisible} onOpenChange={(open) => { if (!open && !start.isPending) start.cancel(); }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{t("apps.start.confirmTitle", { name: start.confirmation?.app.name ?? "" })}</AlertDialogTitle>
          <AlertDialogDescription>{start.confirmation?.unknown ? t("apps.start.unknownFirmware") : t("apps.start.switchFirmware")}</AlertDialogDescription>
        </AlertDialogHeader>
        <ApiErrorAlert error={start.error} />
        <AlertDialogFooter>
          <AlertDialogCancel disabled={start.isPending}>{t("common.cancel")}</AlertDialogCancel>
          <Button disabled={start.isPending} onClick={start.confirm}>{t("apps.start.confirm")}</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
    <AppJobDialog open={start.jobVisible} jobId={start.job?.id ?? null} title={t("apps.start.jobTitle", { name: start.job?.app.name ?? "" })}
      onClose={start.closeJob} onDone={start.finish} onFailed={start.finish} />
  </>;
}
