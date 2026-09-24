import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useGetAppApiV1AppsNameGet } from "@/api/generated/apps/apps";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { appLogsSubtitle } from "@/lib/appLogsSubtitle";
import { appStatusLabel, appStatusTone, isAppStatusBusy } from "@/lib/appStatus";
import { copyText } from "@/lib/copyText";
import { formatLocalTimestamp, formatUtcTimestamp } from "@/lib/formatTimestamp";
import { useAppLogs } from "@/lib/useAppLogs";

/** The full-page app logs view (design doc D18b). Route + `AppShell` chrome live in routes/apps_.$name_.logs.tsx. */
export function AppLogsPanel({ name }: { name: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const { data: app, error } = useGetAppApiV1AppsNameGet(name, {
    query: { refetchInterval: (query) => (query.state.data && isAppStatusBusy(query.state.data.status) ? 3_000 : false) },
  });
  const status = app?.status ?? "stopped";
  const { unavailable, invocations, invocation, setInvocation, accumulated, truncated, text } = useAppLogs(name, status);

  if (error) {
    return <ApiErrorAlert error={error} />;
  }
  if (!app) {
    return <p className="text-sm text-muted-foreground">{t("common.loading")}</p>;
  }

  const tone = appStatusTone(app.status);
  const busy = isAppStatusBusy(app.status);
  // `invocation` is auto-selected to `invocations[0]` once the list loads (see useAppLogs), so
  // `null` (before that load) and a match against the newest entry both mean "showing the
  // current run" -- a stale match against an older list order would otherwise never occur here.
  const isCurrentInvocation = invocation === null || invocation === invocations[0]?.id;
  const subtitle = appLogsSubtitle(t, {
    isCurrentInvocation,
    isLive: isCurrentInvocation && app.status === "running",
  });

  async function handleCopy() {
    const ok = await copyText(text);
    setCopied(ok);
    setCopyFailed(!ok);
    setTimeout(() => { setCopied(false); setCopyFailed(false); }, 2000);
  }

  return (
    <div className="flex min-h-[60vh] flex-1 flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xl font-semibold">{t("appDetail.logsPageTitle", { name: app.name })}</h2>
          <Badge variant={tone === "green" ? "default" : tone === "red" ? "destructive" : tone === "muted" ? "secondary" : "outline"} className="flex items-center gap-1">
            {busy ? <Loader2 className="size-3 animate-spin" /> : null}
            {appStatusLabel(t, app.status, app.exit_code)}
          </Badge>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {invocations.length > 1 && invocation !== null ? (
            <select
              aria-label={t("appDetail.logsInvocationLabel")}
              className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
              value={invocation ?? ""}
              onChange={(event) => setInvocation(event.target.value)}
            >
              {invocations.map((start) => (
                <option key={start.id} value={start.id}>{t("appDetail.logsInvocationAt", { time: formatLocalTimestamp(start.started_at) })}</option>
              ))}
            </select>
          ) : null}
          <Button type="button" variant="outline" size="sm" onClick={() => void handleCopy()}>
            {copied ? t("appDetail.logsCopied") : copyFailed ? t("appDetail.logsCopyFailed") : t("appDetail.logsCopyButton")}
          </Button>
          <Button type="button" variant="outline" size="sm" asChild>
            <Link to="/apps/$name" params={{ name }}>{t("appDetail.logsPageCloseButton")}</Link>
          </Button>
        </div>
      </div>

      {unavailable ? (
        <p className="text-sm text-muted-foreground">{t("appDetail.logsUnavailable")}</p>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">{subtitle}</p>
          {truncated ? (
            <p className="text-sm text-muted-foreground">{t("appDetail.logsTruncated", { count: accumulated.length })}</p>
          ) : null}
          {accumulated.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("appDetail.logsEmptyState")}</p>
          ) : (
            <div className="flex-1 overflow-auto rounded-md bg-muted p-2 font-mono text-xs">
              {accumulated.map((entry, index) => (
                <div key={index} className="flex gap-3">
                  <span className="shrink-0 text-muted-foreground">
                    {entry.timestamp !== null ? formatUtcTimestamp(entry.timestamp, { withYear: false }) : "--"}
                  </span>
                  <span className="whitespace-pre-wrap">{entry.message}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
