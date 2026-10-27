import { useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { getListAppsApiV1AppsGetQueryKey } from "@/api/generated/apps/apps";
import type { AppJobInfo } from "@/api/generated/models";
import type { AddAppTab } from "@/components/AddAppPanel";
import { AddAppPanel } from "@/components/AddAppPanel";
import { AppJobDialog } from "@/components/AppJobDialog";
import { AlertDialog, AlertDialogContent, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

/**
 * Install from a GitHub URL or a zip in one dialog: the source form, then the confirmation in
 * place of it. The progress dialog lives outside it, so closing this one on install keeps the job visible.
 */
export function AddAppDialog({
  open,
  initialTab,
  onOpenChange,
  onInstalled,
}: {
  open: boolean;
  initialTab?: AddAppTab;
  onOpenChange: (open: boolean) => void;
  onInstalled: (appId: string | null) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [installJob, setInstallJob] = useState<{ jobId: string; name: string } | null>(null);

  function handleJobDone(job: AppJobInfo) {
    void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
    setInstallJob(null);
    onInstalled(job.app_id);
  }

  return (
    <>
      <AlertDialog open={open} onOpenChange={onOpenChange}>
        <AlertDialogContent
          className={
            "inset-0 top-0 left-0 h-dvh max-w-none translate-x-0 translate-y-0 content-start overflow-y-auto rounded-none " +
            "sm:inset-auto sm:top-1/2 sm:left-1/2 sm:h-auto sm:max-h-[85dvh] sm:max-w-lg sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-lg"
          }
        >
          <AlertDialogHeader className="flex-row items-center justify-between gap-2 text-left">
            <AlertDialogTitle>{t("appAdd.title")}</AlertDialogTitle>
            <Button type="button" variant="ghost" size="icon" aria-label={t("common.close")} onClick={() => onOpenChange(false)}>
              <X className="size-4" />
            </Button>
          </AlertDialogHeader>
          <AddAppPanel
            tabs={["github", "zip"]}
            initialTab={initialTab}
            inlineConfirm
            onInstallStarted={(jobId, name) => {
              setInstallJob({ jobId, name });
              onOpenChange(false);
            }}
          />
        </AlertDialogContent>
      </AlertDialog>
      <AppJobDialog
        jobId={installJob?.jobId ?? null}
        title={t("appAdd.installingTitle", { name: installJob?.name ?? "" })}
        onClose={() => setInstallJob(null)}
        onDone={handleJobDone}
      />
    </>
  );
}
