import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Loader2, Play, Square } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  getListAppsApiV1AppsGetQueryKey,
  useStartAppEndpointApiV1AppsNameStartPost,
  useStopAppEndpointApiV1AppsNameStopPost,
} from "@/api/generated/apps/apps";
import type { RunningAppInfo } from "@/api/generated/models";
import { RunningAppInfoState } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { Button } from "@/components/ui/button";
import { apiErrorMessage } from "@/lib/apiErrorMessage";
import { cn } from "@/lib/utils";

function ErrorAnnouncement({ message }: { message: string | null }) {
  return message ? (
    <span role="alert" className="sr-only">
      {message}
    </span>
  ) : null;
}

/** The running app with Stop; when idle, the default app with Start, or a link to Apps when there is none. */
export function RunningAppControl({
  runningApp,
  defaultApp,
}: {
  runningApp: RunningAppInfo | null | undefined;
  defaultApp?: { id: string; name: string } | null;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
    void queryClient.invalidateQueries({ queryKey: getListAppsApiV1AppsGetQueryKey() });
  };
  const stopApp = useStopAppEndpointApiV1AppsNameStopPost({ mutation: { onSettled: refresh } });
  const startApp = useStartAppEndpointApiV1AppsNameStartPost({ mutation: { onSettled: refresh } });

  if (!runningApp && defaultApp) {
    const startError = startApp.error ? apiErrorMessage(t, startApp.error) : null;
    return (
      <div className="flex items-center gap-1">
        <Link
          to="/apps/$id"
          params={{ id: defaultApp.id }}
          className="flex min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
        >
          <span className="size-2 shrink-0 rounded-full border border-muted-foreground" aria-hidden />
          <span className="hidden max-w-32 truncate sm:inline">{defaultApp.name}</span>
        </Link>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={cn("gap-1.5", startError && "text-destructive")}
          disabled={startApp.isPending}
          title={startError ?? undefined}
          aria-label={t("header.startApp", { name: defaultApp.name })}
          onClick={() => startApp.mutate({ name: defaultApp.id })}
        >
          {startApp.isPending ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Play className="size-3.5" aria-hidden />}
          <span className="sr-only sm:not-sr-only">{t("header.startAppShort")}</span>
        </Button>
        <ErrorAnnouncement message={startError} />
      </div>
    );
  }

  if (!runningApp) {
    return (
      <Link
        to="/apps"
        className="flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
      >
        <span className="size-2 rounded-full border border-muted-foreground" aria-hidden />
        <span className="sr-only sm:not-sr-only">{t("header.noAppRunning")}</span>
      </Link>
    );
  }

  const stopping = runningApp.state === RunningAppInfoState.stopping || stopApp.isPending;
  const stateLabel =
    runningApp.state === RunningAppInfoState.starting
      ? t("header.appStarting")
      : stopping
        ? t("header.appStopping")
        : null;
  const errorMessage = stopApp.error ? apiErrorMessage(t, stopApp.error) : null;

  return (
    <div className="flex items-center gap-1">
      <Link
        to="/apps/$id"
        params={{ id: runningApp.id }}
        className="flex min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-sm hover:bg-accent hover:text-accent-foreground"
        aria-label={t("header.runningApp", { name: runningApp.name })}
      >
        <span
          className={cn(
            "size-2 shrink-0 rounded-full",
            runningApp.state === RunningAppInfoState.running ? "bg-green-500" : "bg-muted-foreground",
          )}
          aria-hidden
        />
        <span className="hidden max-w-32 truncate font-medium sm:inline">{runningApp.name}</span>
        {stateLabel ? <span className="hidden text-xs text-muted-foreground md:inline">{stateLabel}</span> : null}
      </Link>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn("gap-1.5", errorMessage && "text-destructive")}
        disabled={stopping}
        title={errorMessage ?? undefined}
        onClick={() => stopApp.mutate({ name: runningApp.id })}
      >
        {stopping ? <Loader2 className="size-3.5 animate-spin" aria-hidden /> : <Square className="size-3.5" aria-hidden />}
        <span className="sr-only sm:not-sr-only">{t("header.stopApp")}</span>
      </Button>
      <ErrorAnnouncement message={errorMessage} />
    </div>
  );
}
