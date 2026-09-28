import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Loader2, Package, Plus } from "lucide-react";
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
import { appStatusLabel, appStatusTone, isAppJobStatus, isAppStatusBusy } from "@/lib/appStatus";
import { formatSourceSummary } from "@/lib/appSourceSummary";
import { usePlatformUpdate } from "@/lib/usePlatformUpdate";

/** Poll cadence while any app is mid-transition (design doc 3.7). */
const ACTIVE_POLL_INTERVAL_MS = 5_000;

export function AppsListPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data, error, isLoading } = useListAppsApiV1AppsGet({
    query: {
      refetchInterval: (query) => {
        const apps = query.state.data?.apps ?? [];
        return apps.some((app) => isAppStatusBusy(app.status)) ? ACTIVE_POLL_INTERVAL_MS : false;
      },
    },
  });
  const { platform, startUpdate, starting, startError, job: platformJob } = usePlatformUpdate();
  const [repairJob, setRepairJob] = useState<{ jobId: string; name: string } | null>(null);
  const [resetDialogOpen, setResetDialogOpen] = useState(false);
  const [resetLeftovers, setResetLeftovers] = useState<string[]>([]);

  const invalidateList = () => void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });

  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSuccess: invalidateList } });
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSuccess: invalidateList } });
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
      {platform && !ready ? (
        <Alert>
          <AlertTitle>{t("apps.platformBannerTitle")}</AlertTitle>
          <AlertDescription className="flex flex-col gap-3">
            <span>
              {platform.latest?.summary
                ? t("apps.platformBannerBodyWithSummary", { summary: platform.latest.summary })
                : t("apps.platformBannerBody")}
            </span>
            <ApiErrorAlert error={startError} />
            <Button className="w-fit" onClick={startUpdate} disabled={starting || platformJob?.state === "running"}>
              {t("apps.platformUpdateButton")}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">{t("apps.title")}</p>
        <Button asChild>
          <Link to="/apps/add">
            <Plus className="size-4" />
            {t("apps.addAppButton")}
          </Link>
        </Button>
      </div>

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
        <p className="text-sm text-muted-foreground">{t("apps.emptyState")}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {apps.map((app) => (
            <AppRow
              key={app.id}
              app={app}
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
          {app.autostart ? <Badge variant="outline">{t("apps.autostartBadge")}</Badge> : null}
          {app.update_available ? <Badge variant="outline">{t("apps.updateAvailableChip")}</Badge> : null}
          {app.credential_rejected ? <Badge variant="destructive">{t("apps.credentialRejectedBadge")}</Badge> : null}
        </div>
        {app.name !== appIdNamePart(app.id) ? <p className="truncate text-xs text-muted-foreground">{app.name}</p> : null}
        <p className="truncate text-xs text-muted-foreground">{formatSourceSummary(app.source)}</p>
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
