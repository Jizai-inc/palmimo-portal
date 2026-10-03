import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useGetAppApiV1AppsNameGet } from "@/api/generated/apps/apps";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { LogViewer } from "@/components/LogViewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { appLogsSubtitle } from "@/lib/appLogsSubtitle";
import { appStatusLabel, appStatusTone, isAppStatusBusy } from "@/lib/appStatus";
import { useAppLogs } from "@/lib/useAppLogs";

/** The full-page app logs view (design doc D18b). Route + `AppShell` chrome live in routes/apps_.$id_.logs.tsx. */
export function AppLogsPanel({ id }: { id: string }) {
  const { t } = useTranslation();

  const { data: app, error } = useGetAppApiV1AppsNameGet(id, {
    query: { refetchInterval: (query) => (query.state.data && isAppStatusBusy(query.state.data.status) ? 3_000 : false) },
  });
  const status = app?.status ?? "stopped";
  const { unavailable, invocations, invocation, setInvocation, isCurrentInvocation, accumulated, droppedCount, truncated, text } = useAppLogs(id, status);

  if (error) {
    return <ApiErrorAlert error={error} />;
  }
  if (!app) {
    return <p className="text-sm text-muted-foreground">{t("common.loading")}</p>;
  }

  const tone = appStatusTone(app.status);
  const busy = isAppStatusBusy(app.status);
  const subtitle = appLogsSubtitle(t, {
    isCurrentInvocation,
    isLive: isCurrentInvocation && app.status === "running",
  });

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xl font-semibold">{t("appDetail.logsPageTitle", { name: app.name })}</h2>
          <Badge variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"} className="flex items-center gap-1">
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
        </div>
      </div>

      {!unavailable ? (
        <>
          <p className="text-sm text-muted-foreground">{subtitle}</p>
          <p className="text-xs text-muted-foreground">{t("appDetail.logsReusedIdNote")}</p>
          {truncated ? (
            <p className="text-sm text-muted-foreground">{t("appDetail.logsTruncated", { count: accumulated.length })}</p>
          ) : null}
        </>
      ) : null}
      <LogViewer
        droppedCount={droppedCount}
        entries={accumulated}
        text={text}
        expanded
        unavailable={!!unavailable}
        invocations={invocations}
        invocation={invocation}
        setInvocation={setInvocation}
        toolbar={
          <Button type="button" variant="outline" size="sm" asChild>
            <Link to="/apps/$id" params={{ id }}>{t("appDetail.logsPageCloseButton")}</Link>
          </Button>
        }
      />
    </div>
  );
}
