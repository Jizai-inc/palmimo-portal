import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Info, Loader2, Package } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getListAppsApiV1AppsGetQueryKey,
  useListAppsApiV1AppsGet,
  useResetAppsApiV1AppsResetPost,
  useStartAppEndpointApiV1AppsNameStartPost,
  useStopAppEndpointApiV1AppsNameStopPost,
  useUpdateAppApiV1AppsNameUpdatePost,
} from "@/api/generated/apps/apps";
import type { AppSummary, PalmimoPortalApiAppsResetResponse } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppIdLabel, appIdNamePart } from "@/components/AppIdLabel";
import { AppJobDialog } from "@/components/AppJobDialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { appStatusLabel, appStatusTone, isAppJobStatus, isAppStatusBusy, shouldPollAppStatus } from "@/lib/appStatus";
import { formatSourceSummary } from "@/lib/appSourceSummary";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";

/** Poll cadence while any app is mid-transition (design doc 3.7) or running (see `shouldPollAppStatus`). */
const ACTIVE_POLL_INTERVAL_MS = 5_000;

export function AppsListPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useListAppsApiV1AppsGet({
    query: {
      refetchInterval: (query) => {
        const apps = query.state.data?.apps ?? [];
        return apps.some((app) => app.wifi_setup_preempted || shouldPollAppStatus(app.status)) ? ACTIVE_POLL_INTERVAL_MS : false;
      },
    },
  });
  const { platform, startUpdate, starting, startError, job: platformJob } = usePlatformUpdate();
  const [repairJob, setRepairJob] = useState<{ jobId: string; name: string } | null>(null);
  const [resetDialogOpen, setResetDialogOpen] = useState(false);
  const [resetLeftovers, setResetLeftovers] = useState<string[]>([]);

  const invalidateList = () => void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
  const invalidateListAndStatus = () => {
    invalidateList();
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
  };

  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSuccess: invalidateListAndStatus } });
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSuccess: invalidateListAndStatus } });
  const repairApp = useUpdateAppApiV1AppsNameUpdatePost();
  const resetApps = useResetAppsApiV1AppsResetPost({
    mutation: {
      onSuccess: (data: PalmimoPortalApiAppsResetResponse) => {
        setResetLeftovers(data.leftover_paths);
        if (data.leftover_paths.length === 0) {
          setResetDialogOpen(false);
        }
        void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
      },
    },
  });

  const ready = platform?.ready ?? true;
  const apps = data?.apps ?? [];
  const jobInProgress = apps.some((app) => isAppJobStatus(app.status));
  const ledgerRecovery =
    error instanceof PortalApiError && (error.code === "ledger_legacy" || error.code === "platform_state_corrupt")
      ? error.code
      : null;
  const isCorruptLedger = ledgerRecovery === "platform_state_corrupt";
  const ledgerTitle = isCorruptLedger ? t("apps.corruptLedgerTitle") : t("apps.legacyLedgerTitle");
  const ledgerBody = isCorruptLedger ? t("apps.corruptLedgerBody") : t("apps.legacyLedgerBody");
  const ledgerResetButton = isCorruptLedger ? t("apps.corruptLedgerResetButton") : t("apps.legacyLedgerResetButton");
  const ledgerDialogTitle = isCorruptLedger ? t("apps.corruptLedgerDialogTitle") : t("apps.legacyLedgerDialogTitle");
  const ledgerDialogBody = isCorruptLedger ? t("apps.corruptLedgerDialogBody") : t("apps.legacyLedgerDialogBody");
  const ledgerDialogConfirm = isCorruptLedger ? t("apps.corruptLedgerDialogConfirm") : t("apps.legacyLedgerDialogConfirm");

  return (
    <div className="flex flex-col gap-4">
      {platformJob?.state === "running" && platformJob.step === "display_firmware" ? (
        <Alert><AlertDescription>{t("update.displayFirmwareWriting")}</AlertDescription></Alert>
      ) : null}
      {platform && !ready ? (
        <div className="flex flex-col gap-3 rounded-xl border border-warning-border bg-warning-soft p-4 text-sm text-warning-foreground sm:flex-row sm:items-center">
          <Info className="hidden size-4 shrink-0 sm:block" aria-hidden />
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <p className="font-semibold">{t("apps.platformBannerTitle")}</p>
            <p className="text-[13px]">
              {platform.latest?.summary
                ? t("apps.platformBannerBodyWithSummary", { summary: platform.latest.summary })
                : t("apps.platformBannerBody")}
            </p>
            <ApiErrorAlert error={startError} />
          </div>
          <Button size="sm" className="w-fit shrink-0" onClick={startUpdate} disabled={starting || platformJob?.state === "running"}>
            {t("apps.platformUpdateButton")}
          </Button>
        </div>
      ) : null}

      {ledgerRecovery ? null : <ApiErrorAlert error={error} />}

      {ledgerRecovery ? (
        <Alert>
          <AlertTitle>{ledgerTitle}</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span>{ledgerBody}</span>
            <Button className="w-fit" variant="destructive" onClick={() => setResetDialogOpen(true)}>
              {ledgerResetButton}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {error ? null : isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : apps.length === 0 ? (
        <div className="flex flex-col items-center gap-3.5 rounded-[14px] border border-dashed border-border-strong px-6 py-16 text-center">
          <span className="flex size-12 items-center justify-center rounded-xl bg-secondary" aria-hidden>
            <Package className="size-[22px] text-muted-foreground" />
          </span>
          <p className="text-[17px] font-semibold">{t("apps.emptyState")}</p>
          <p className="max-w-[420px] text-sm leading-normal text-muted-foreground">{t("apps.emptyStateBody")}</p>
          <Button asChild className="mt-1.5">
            <Link to="/apps" search={{ tab: "discover" }}>
              {t("apps.browseOfficialApps")}
            </Link>
          </Button>
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {apps.map((app) => (
            <AppRow
              key={app.id}
              app={app}
              isDefault={data?.default_app === app.id}
              disabledForPlatform={!ready}
              disabledForJob={jobInProgress}
              onStart={() => startApp.mutate({ name: app.id })}
              onStop={() => stopApp.mutate({ name: app.id })}
              onRepair={() =>
                app.source.type === "git"
                  ? repairApp.mutate({ name: app.id }, { onSuccess: (data) => setRepairJob({ jobId: data.job.id, name: app.name }) })
                  : undefined
              }
              startPending={startApp.isPending && startApp.variables?.name === app.id}
              stopPending={stopApp.isPending && stopApp.variables?.name === app.id}
              rowError={
                startApp.variables?.name === app.id
                  ? startApp.error
                  : stopApp.variables?.name === app.id
                    ? stopApp.error
                    : undefined
              }
            />
          ))}
        </ul>
      )}

      <AppJobDialog
        jobId={repairJob?.jobId ?? null}
        title={t("apps.jobDialogTitle", { name: repairJob?.name ?? "" })}
        onClose={() => setRepairJob(null)}
        onDone={invalidateList}
      />
      <AlertDialog
        open={resetDialogOpen}
        onOpenChange={(open) => {
          setResetDialogOpen(open);
          if (open) setResetLeftovers([]);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{ledgerDialogTitle}</AlertDialogTitle>
            <AlertDialogDescription>{ledgerDialogBody}</AlertDialogDescription>
          </AlertDialogHeader>
          {resetLeftovers.length > 0 ? (
            <Alert variant="destructive">
              <AlertTitle>{t("apps.resetLeftoversTitle")}</AlertTitle>
              <AlertDescription className="flex flex-col gap-2">
                <span>{t("apps.resetLeftoversBody")}</span>
                <ul className="list-inside list-disc">{resetLeftovers.map((path) => <li key={path}>{path}</li>)}</ul>
              </AlertDescription>
            </Alert>
          ) : null}
          <ApiErrorAlert error={resetApps.error} />
          <AlertDialogFooter>
            <AlertDialogCancel disabled={resetApps.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button variant="destructive" disabled={resetApps.isPending} onClick={() => resetApps.mutate()}>
              {ledgerDialogConfirm}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function AppRow({
  app,
  isDefault,
  disabledForPlatform,
  disabledForJob,
  onStart,
  onStop,
  onRepair,
  startPending,
  stopPending,
  rowError,
}: {
  app: AppSummary;
  isDefault: boolean;
  disabledForPlatform: boolean;
  disabledForJob: boolean;
  onStart: () => void;
  onStop: () => void;
  onRepair: () => void;
  startPending: boolean;
  stopPending: boolean;
  rowError: unknown;
}) {
  const { t } = useTranslation();
  const tone = appStatusTone(app.status);
  const busy = isAppStatusBusy(app.status);
  const needsRepair = app.status === "needs_repair" || app.status === "broken";
  const startDisabledTitle = disabledForPlatform
    ? t("apps.platformActionDisabledTooltip")
    : disabledForJob
      ? t("apps.jobInProgressDisabledTooltip")
      : undefined;

  return (
    <li className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 md:flex-row md:items-center md:gap-4">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted">
        <Package className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Link to="/apps/$id" params={{ id: app.id }} className="min-w-0 truncate hover:underline">
            <AppIdLabel id={app.id} />
          </Link>
          <Badge
            variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"}
            className="flex items-center gap-1"
          >
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
          {app.source.official ? <Badge>{t("appAdd.catalogOfficialBadge")}</Badge> : null}
          {isDefault ? <Badge variant="outline">{t("apps.defaultBadge")}</Badge> : null}
          {app.missing_env && app.missing_env.length > 0 ? (
            <Link to="/apps/$id" params={{ id: app.id }}>
              <Badge variant="destructive">{t("apps.missingKeysBadge")}</Badge>
            </Link>
          ) : null}
          {app.update_available ? <Badge variant="outline">{t("apps.updateAvailableChip")}</Badge> : null}
          {app.credential_rejected ? <Badge variant="destructive">{t("apps.credentialRejectedBadge")}</Badge> : null}
        </div>
        {app.name !== appIdNamePart(app.id) ? <p className="truncate text-xs text-muted-foreground">{app.name}</p> : null}
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{formatSourceSummary(app.source)}</p>
        {app.wifi_setup_preempted ? (
          <p className="text-sm text-muted-foreground">{t("apps.wifiSetupPreempted")}</p>
        ) : null}
        <ApiErrorAlert error={rowError} />
      </div>
      <div className="flex shrink-0 flex-wrap gap-2" title={startDisabledTitle}>
        {app.status === "running" ? (
          <>
            {app.url ? (
              <Button variant="outline" asChild>
                <a href={app.url} target="_blank" rel="noreferrer">
                  {t("apps.openButton")}
                </a>
              </Button>
            ) : null}
            <Button variant="outline" onClick={onStop} disabled={stopPending}>
              {t("apps.stopButton")}
            </Button>
          </>
        ) : app.status === "stopped" || app.status === "failed" ? (
          <Button onClick={onStart} disabled={startPending || disabledForPlatform || disabledForJob}>
            {t("apps.startButton")}
          </Button>
        ) : needsRepair ? (
          app.source.type === "git" ? (
            <Button variant="outline" onClick={onRepair} disabled={disabledForPlatform}>
              {t("apps.repairButton")}
            </Button>
          ) : (
            <Button variant="outline" asChild>
              <Link to="/apps/$id" params={{ id: app.id }}>
                {t("apps.repairButton")}
              </Link>
            </Button>
          )
        ) : null}
      </div>
    </li>
  );
}
