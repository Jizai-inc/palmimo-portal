import type { TFunction } from "i18next";

import { AppSummaryStatus } from "@/api/generated/models";

/** Badge visual treatment for one app status (design brief: running=green, stopped=outline, failed/broken/needs_repair=red, transitional=muted). */
export type AppStatusTone = "green" | "outline" | "muted" | "red";

const TONE_BY_STATUS: Record<string, AppStatusTone> = {
  [AppSummaryStatus.running]: "green",
  [AppSummaryStatus.stopped]: "outline",
  [AppSummaryStatus.starting]: "muted",
  [AppSummaryStatus.stopping]: "muted",
  [AppSummaryStatus.installing]: "muted",
  [AppSummaryStatus.updating]: "muted",
  [AppSummaryStatus.deleting]: "muted",
  [AppSummaryStatus.failed]: "red",
  [AppSummaryStatus.needs_repair]: "red",
  [AppSummaryStatus.broken]: "red",
};

export function appStatusTone(status: string): AppStatusTone {
  return TONE_BY_STATUS[status] ?? "outline";
}

/** Whether `status` reflects an in-flight `apps.lock` job the UI should show a spinner for. */
export function isAppStatusBusy(status: string): boolean {
  return (
    status === AppSummaryStatus.starting ||
    status === AppSummaryStatus.stopping ||
    status === AppSummaryStatus.installing ||
    status === AppSummaryStatus.updating ||
    status === AppSummaryStatus.deleting
  );
}

/**
 * Whether `status` is an install/update/delete job status -- the same `apps.lock` job the
 * backend's `app_job_in_progress` start precheck refuses every app's start against (design
 * doc 3.10: its sync step runs untrusted build hooks as the same uid an app unit runs as).
 * Exactly one app in the list can have one of these statuses at a time.
 */
export function isAppJobStatus(status: string): boolean {
  return (
    status === AppSummaryStatus.installing ||
    status === AppSummaryStatus.updating ||
    status === AppSummaryStatus.deleting
  );
}

/**
 * Translated label for one app's status, via explicit `t("apps.status…")` calls so the
 * i18n-parity static scan (which only recognizes literal `t(...)` calls, see
 * `src/lib/navLabels.ts` for the same pattern) can see every key used here.
 */
export function appStatusLabel(t: TFunction, status: string, exitCode: number | null | undefined): string {
  switch (status) {
    case AppSummaryStatus.running:
      return t("apps.statusRunning");
    case AppSummaryStatus.stopped:
      return t("apps.statusStopped");
    case AppSummaryStatus.starting:
      return t("apps.statusStarting");
    case AppSummaryStatus.stopping:
      return t("apps.statusStopping");
    case AppSummaryStatus.installing:
      return t("apps.statusInstalling");
    case AppSummaryStatus.updating:
      return t("apps.statusUpdating");
    case AppSummaryStatus.deleting:
      return t("apps.statusDeleting");
    case AppSummaryStatus.needs_repair:
      return t("apps.statusNeedsRepair");
    case AppSummaryStatus.broken:
      return t("apps.statusBroken");
    case AppSummaryStatus.failed:
      return exitCode != null ? t("apps.statusFailedWithExitCode", { exitCode }) : t("apps.statusFailed");
    default:
      return status;
  }
}

/** Translated label for one install/update/delete job step, via explicit `t(...)` calls (same reason as {@link appStatusLabel}). */
export function appJobStepLabel(t: TFunction, step: string | null): string {
  switch (step) {
    case "fetch":
      return t("apps.jobStepFetch");
    case "extract":
      return t("apps.jobStepExtract");
    case "validate":
      return t("apps.jobStepValidate");
    case "sync":
      return t("apps.jobStepSync");
    case "swap":
      return t("apps.jobStepSwap");
    case "register":
      return t("apps.jobStepRegister");
    default:
      return step ?? "";
  }
}
