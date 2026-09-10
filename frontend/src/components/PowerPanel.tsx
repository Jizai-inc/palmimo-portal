import { Power, RotateCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getStatusApiV1SystemStatusGet, useRebootApiV1SystemRebootPost, useShutdownApiV1SystemShutdownPost } from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { CenteredState } from "@/components/CenteredState";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";

/** How often to re-poll `system/status` while waiting for a reboot to complete (see routes/wifi.waiting.tsx for the same pattern). */
const DEFAULT_POLL_INTERVAL_MS = 4_000;

type PanelState = "idle" | "rebooting" | "shuttingDown";
type DialogKind = "restart" | "shutdown" | null;

/**
 * The power-controls screen's logic (see routes/power.tsx, which wraps this
 * in `AppShell`). Free of router hooks -- `onRebooted` is the only reach-out
 * to routing, and the poll interval is injectable for tests.
 */
export function PowerPanel({
  onRebooted,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  /** Called once a reboot is detected complete, or from the manual "Reopen the Portal" fallback. The route passes `() => navigate({ to: "/" })`. */
  onRebooted: () => void;
  /** Injectable so tests do not have to wait out the real interval. */
  pollIntervalMs?: number;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<PanelState>("idle");
  const [dialogKind, setDialogKind] = useState<DialogKind>(null);

  // An update job actually applying must not be interrupted by a voluntary
  // reboot/shutdown -- the backend enforces this with a 409
  // (api/system.py's `_refuse_while_updating`); this disables the buttons
  // up front so the operator sees why. An ordinary query suffices since
  // this only needs to notice the state, not drive a state machine off it.
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet({
    query: {
      refetchInterval: (query) => (query.state.data?.job.state === "running" ? 2_000 : false),
    },
  });
  const updateInProgress = updateStatus?.job.state === "running";

  const reboot = useRebootApiV1SystemRebootPost({
    mutation: {
      onSuccess: () => {
        setDialogKind(null);
        setState("rebooting");
      },
      onError: () => setDialogKind(null),
    },
  });
  const shutdown = useShutdownApiV1SystemShutdownPost({
    mutation: {
      onSuccess: () => {
        setDialogKind(null);
        setState("shuttingDown");
      },
      onError: () => setDialogKind(null),
    },
  });

  // `onRebooted` is read through a ref so the polling effect below does not
  // restart just because the route passed a fresh arrow-function identity.
  const onRebootedRef = useRef(onRebooted);
  onRebootedRef.current = onRebooted;

  // Whether a poll has failed at least once this reboot. Kept in a ref so it
  // survives the effect being torn down and re-run by an unrelated re-render.
  const hasFailedRef = useRef(false);

  // Polls `system/status` directly (bypassing TanStack Query, same as
  // routes/wifi.waiting.tsx), swallowing failures since the device being
  // down is expected mid-reboot. Complete only once a poll has failed and
  // then succeeded again, so an early still-succeeding poll is not
  // mistaken for completion. Sequential via `setTimeout`, not
  // `setInterval`, so a slow poll never overlaps the next tick; each poll
  // carries its own `AbortSignal.timeout` so a hung TCP handshake against a
  // rebooting host still registers as a failure instead of never resolving.
  useEffect(() => {
    if (state !== "rebooting") return;

    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    hasFailedRef.current = false;

    async function poll() {
      if (cancelled) return;
      try {
        await getStatusApiV1SystemStatusGet({ signal: AbortSignal.timeout(pollIntervalMs) });
        if (cancelled) return;
        if (hasFailedRef.current) {
          // Stop polling before calling out, so a slow `onRebooted` cannot
          // race a later tick into reporting a second time.
          cancelled = true;
          onRebootedRef.current();
          return;
        }
      } catch {
        if (!cancelled) {
          hasFailedRef.current = true;
        }
      }
      if (!cancelled) {
        timeoutId = setTimeout(() => void poll(), pollIntervalMs);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [state, pollIntervalMs]);

  const mutationPending = reboot.isPending || shutdown.isPending;
  const controlsDisabled = mutationPending || updateInProgress;

  return (
    <div className="flex flex-col gap-4">
      {state === "idle" ? (
        <>
          <p className="text-sm text-muted-foreground">{t("power.body")}</p>
          {updateInProgress ? (
            <Alert>
              <AlertDescription>{t("power.updateInProgress")}</AlertDescription>
            </Alert>
          ) : null}
          <ApiErrorAlert error={reboot.error ?? shutdown.error} />
          <div className="flex flex-col gap-4 md:flex-row">
            <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 md:w-[400px]">
              <div className="flex items-center gap-2">
                <RotateCw className="size-5 text-destructive" />
                <p className="font-semibold">{t("power.restart")}</p>
              </div>
              <p className="text-sm text-muted-foreground">{t("power.restartCardDescription")}</p>
              <Button
                variant="outline"
                className="w-full md:w-fit"
                onClick={() => setDialogKind("restart")}
                disabled={controlsDisabled}
              >
                {t("power.restart")}
              </Button>
            </div>
            <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4 md:w-[400px]">
              <div className="flex items-center gap-2">
                <Power className="size-5 text-destructive" />
                <p className="font-semibold">{t("power.shutdown")}</p>
              </div>
              <p className="text-sm text-muted-foreground">{t("power.shutdownCardDescription")}</p>
              <Button
                variant="destructive"
                className="w-full md:w-fit"
                onClick={() => setDialogKind("shutdown")}
                disabled={controlsDisabled}
              >
                {t("power.shutdown")}
              </Button>
            </div>
          </div>
        </>
      ) : state === "rebooting" ? (
        <CenteredState
          icon={RotateCw}
          title={t("power.rebootingTitle")}
          body={t("power.rebootingBody")}
          showProgress
          progressLabel={t("common.inProgress")}
          action={
            <Button variant="outline" onClick={onRebooted}>
              {t("power.reopenPortal")}
            </Button>
          }
        />
      ) : (
        <CenteredState icon={Power} title={t("power.shuttingDownTitle")} body={t("power.shuttingDownBody")} />
      )}

      <AlertDialog open={dialogKind !== null} onOpenChange={(open) => !open && setDialogKind(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{dialogKind === "restart" ? t("power.restartDialogTitle") : t("power.shutdownDialogTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {dialogKind === "restart" ? t("power.restartDialogBody") : t("power.shutdownDialogBody")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutationPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={mutationPending}
              onClick={() => (dialogKind === "restart" ? reboot.mutate(undefined) : shutdown.mutate(undefined))}
            >
              {dialogKind === "restart" ? t("power.restartDialogConfirm") : t("power.shutdownDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
