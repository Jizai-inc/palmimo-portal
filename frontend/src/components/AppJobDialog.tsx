import { useEffect } from "react";
import { useTranslation } from "react-i18next";

import { useGetJobApiV1AppsJobsJobIdGet } from "@/api/generated/apps/apps";
import { PortalApiError } from "@/api/client";
import type { AppJobInfo } from "@/api/generated/models";
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ProgressBar } from "@/components/ProgressBar";
import { appJobStepLabel } from "@/lib/appStatus";

/** How often to re-poll a running install/update/delete job (design doc 3.7's log-poll cadence applies to jobs too). */
const JOB_POLL_INTERVAL_MS = 1_000;

/**
 * The install/update/delete job-progress modal (design doc 3.7): polls `GET /apps/jobs/{id}`
 * while the job is running, shows the current step, and calls {@link onDone}/{@link onFailed}
 * once it settles. Closing the dialog (via {@link onClose}) does not cancel the job -- the note
 * under the progress bar says so explicitly, since there is no cancel endpoint.
 */
export function AppJobDialog({
  jobId,
  title,
  onClose,
  onDone,
}: {
  jobId: string | null;
  title: string;
  onClose: () => void;
  /** Called once, the first render the job is observed `done`. */
  onDone: (job: AppJobInfo) => void;
}) {
  const { t } = useTranslation();
  const { data: job, error } = useGetJobApiV1AppsJobsJobIdGet(jobId ?? "", {
    query: {
      enabled: jobId !== null,
      // A job unknown to the Portal (404) is terminal, the same as "failed" -- without also
      // checking query status here, react-query keeps the last-seen "running" data around on a
      // fetch error, and this callback (which only ever sees `query.state.data`) would poll it forever.
      refetchInterval: (query) =>
        query.state.status !== "error" && query.state.data?.state === "running" ? JOB_POLL_INTERVAL_MS : false,
    },
  });
  const jobUnknown = error instanceof PortalApiError && error.code === "job_not_found";

  // Fires once per job reaching "done" (keyed on the job's own id, not just `state`, so a
  // second job reusing this same dialog instance fires again).
  useEffect(() => {
    if (job?.state === "done") {
      onDone(job);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, job?.state]);

  return (
    <AlertDialog open={jobId !== null} onOpenChange={(open) => !open && onClose()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
        </AlertDialogHeader>
        {jobUnknown ? (
          <Alert variant="destructive">
            <AlertDescription>{t("apps.jobUnknown")}</AlertDescription>
          </Alert>
        ) : job?.state === "failed" ? (
          <Alert variant="destructive">
            <AlertDescription>
              {job.step ? `${appJobStepLabel(t, job.step)}: ` : ""}
              {job.error ?? t("errors.install_failed")}
            </AlertDescription>
          </Alert>
        ) : (
          <div className="flex flex-col gap-2">
            <p className="text-sm">{appJobStepLabel(t, job?.step ?? null)}</p>
            <ProgressBar label={t("common.inProgress")} />
          </div>
        )}
        <p className="text-xs text-muted-foreground">{t("apps.jobDialogCloseNote")}</p>
        <AlertDialogFooter>
          <Button variant="outline" onClick={onClose}>
            {t("apps.jobClose")}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
