import type { TFunction } from "i18next";
import { Power, RotateCw, TriangleAlert } from "lucide-react";
import type * as React from "react";
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

/** Posture first: the robot must be on its stand before the servos stop holding. */
const SHUTDOWN_CHECKS = ["onStand", "servoOff"] as const;
type ShutdownCheck = (typeof SHUTDOWN_CHECKS)[number];

/** Via explicit `t(...)` calls so the i18n-parity scan sees every key (see `navLabels.ts`). */
function shutdownCheckLabel(t: TFunction, check: ShutdownCheck): string {
  return check === "onStand" ? t("power.checkOnStand") : t("power.checkServoOff");
}

function PowerRow({
  icon,
  title,
  description,
  action,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  action: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 border-border px-5 py-4.5 sm:flex-row sm:items-center sm:gap-4 border-t first:border-t-0">
      <div className="flex min-w-0 flex-1 items-start gap-4 sm:items-center">
        {icon}
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="text-sm font-semibold">{title}</p>
          <p className="text-[13px] text-muted-foreground">{description}</p>
        </div>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}

/**
 * The power-controls screen's logic (see routes/settings.device.tsx). Free of router hooks -- `onRebooted` is the only reach-out
 * to routing, and the poll interval is injectable for tests.
 */
export function PowerPanel({
  onRebooted,
  initialDialog,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
}: {
  /** Called once a reboot is detected complete, or from the manual "Reopen the Portal" fallback. The route passes `() => navigate({ to: "/" })`. */
  onRebooted: () => void;
  /** Opens that confirm dialog on mount -- the header's device menu lands here with it set. */
  initialDialog?: Exclude<DialogKind, null>;
  /** Injectable so tests do not have to wait out the real interval. */
  pollIntervalMs?: number;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<PanelState>("idle");
  const [dialogKind, setDialogKind] = useState<DialogKind>(initialDialog ?? null);
  const [checked, setChecked] = useState<ShutdownCheck[]>([]);

  function openDialog(kind: Exclude<DialogKind, null>) {
    setChecked([]);
    setDialogKind(kind);
  }

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
          {updateInProgress ? (
            <Alert>
              <AlertDescription>{t("power.updateInProgress")}</AlertDescription>
            </Alert>
          ) : null}
          <ApiErrorAlert error={reboot.error ?? shutdown.error} />
          <div className="overflow-hidden rounded-xl border border-border bg-card">
            <PowerRow
              icon={<RotateCw className="size-[18px] text-muted-foreground" aria-hidden />}
              title={t("power.restart")}
              description={t("power.restartCardDescription")}
              action={
                <Button variant="outline" size="sm" onClick={() => openDialog("restart")} disabled={controlsDisabled}>
                  {t("power.restart")}
                </Button>
              }
            />
            <PowerRow
              icon={<Power className="size-[18px] text-danger-foreground" aria-hidden />}
              title={t("power.shutdown")}
              description={t("power.shutdownCardDescription")}
              action={
                <Button variant="dangerOutline" size="sm" onClick={() => openDialog("shutdown")} disabled={controlsDisabled}>
                  {t("power.shutdown")}
                </Button>
              }
            />
          </div>
          <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
            <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
            {t("power.unplugWarning")}
          </p>
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
              {dialogKind === "restart" ? t("power.restartDialogBody") : t("power.shutdownChecklistIntro")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {dialogKind === "shutdown" ? (
            <div className="flex flex-col gap-4">
              <ul className="flex flex-col gap-2.5 text-sm">
                {SHUTDOWN_CHECKS.map((check) => (
                  <li key={check}>
                    <label className="flex cursor-pointer items-center gap-2.5">
                      <input
                        type="checkbox"
                        className="size-[18px] shrink-0 cursor-pointer rounded-[5px] accent-foreground"
                        checked={checked.includes(check)}
                        onChange={(event) =>
                          setChecked((previous) => (event.target.checked ? [...previous, check] : previous.filter((c) => c !== check)))
                        }
                      />
                      {shutdownCheckLabel(t, check)}
                    </label>
                  </li>
                ))}
              </ul>
              <p className="text-xs text-muted-foreground">{t("power.shutdownAfterNote")}</p>
            </div>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mutationPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              variant="destructive"
              disabled={mutationPending || (dialogKind === "shutdown" && checked.length < SHUTDOWN_CHECKS.length)}
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
