import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Loader2, Package, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getListAppsApiV1AppsGetQueryKey,
  useListAppsApiV1AppsGet,
  useStartAppEndpointApiV1AppsNameStartPost,
  useStopAppEndpointApiV1AppsNameStopPost,
  useUpdateAppApiV1AppsNameUpdatePost,
} from "@/api/generated/apps/apps";
import type { AppSummary } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AppJobDialog } from "@/components/AppJobDialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { appStatusLabel, appStatusTone, isAppStatusBusy } from "@/lib/appStatus";
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

  const invalidateList = () => void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });

  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSuccess: invalidateList } });
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSuccess: invalidateList } });
  const repairApp = useUpdateAppApiV1AppsNameUpdatePost({
    mutation: { onSuccess: (data, variables) => setRepairJob({ jobId: data.job.id, name: variables.name }) },
  });

  const ready = platform?.ready ?? true;
  const apps = data?.apps ?? [];

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

      <ApiErrorAlert error={error} />

      {error ? null : isLoading ? (
        <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
      ) : apps.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("apps.emptyState")}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {apps.map((app) => (
            <AppRow
              key={app.name}
              app={app}
              disabledForPlatform={!ready}
              onStart={() => startApp.mutate({ name: app.name })}
              onStop={() => stopApp.mutate({ name: app.name })}
              onRepair={() =>
                app.source.type === "git" ? repairApp.mutate({ name: app.name }) : undefined
              }
              startPending={startApp.isPending && startApp.variables?.name === app.name}
              stopPending={stopApp.isPending && stopApp.variables?.name === app.name}
              rowError={
                startApp.variables?.name === app.name
                  ? startApp.error
                  : stopApp.variables?.name === app.name
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
    </div>
  );
}

function AppRow({
  app,
  disabledForPlatform,
  onStart,
  onStop,
  onRepair,
  startPending,
  stopPending,
  rowError,
}: {
  app: AppSummary;
  disabledForPlatform: boolean;
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

  return (
    <li className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 md:flex-row md:items-center md:gap-4">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted">
        <Package className="size-5" />
      </span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Link to="/apps/$name" params={{ name: app.name }} className="min-w-0 truncate font-medium hover:underline">
            {app.name}
          </Link>
          <Badge
            variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"}
            className="flex items-center gap-1"
          >
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
          {app.autostart ? <Badge variant="outline">{t("apps.autostartBadge")}</Badge> : null}
          {app.update_available ? <Badge variant="outline">{t("apps.updateAvailableChip")}</Badge> : null}
          {app.credential_rejected ? <Badge variant="destructive">{t("apps.credentialRejectedBadge")}</Badge> : null}
        </div>
        <ApiErrorAlert error={rowError} />
      </div>
      <div className="flex shrink-0 flex-wrap gap-2" title={disabledForPlatform ? t("apps.platformActionDisabledTooltip") : undefined}>
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
          <Button onClick={onStart} disabled={startPending || disabledForPlatform}>
            {t("apps.startButton")}
          </Button>
        ) : needsRepair ? (
          app.source.type === "git" ? (
            <Button variant="outline" onClick={onRepair} disabled={disabledForPlatform}>
              {t("apps.repairButton")}
            </Button>
          ) : (
            <Button variant="outline" asChild>
              <Link to="/apps/$name" params={{ name: app.name }}>
                {t("apps.repairButton")}
              </Link>
            </Button>
          )
        ) : null}
      </div>
    </li>
  );
}
