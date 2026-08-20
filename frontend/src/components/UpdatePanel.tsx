import { useQueryClient } from "@tanstack/react-query";
import type { TFunction } from "i18next";
import { Download } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import {
  getGetStatusApiV1UpdateStatusGetQueryKey,
  useApplyApiV1UpdateApplyPost,
  useCheckApiV1UpdateCheckPost,
  useGetStatusApiV1UpdateStatusGet,
  useRollbackApiV1UpdateRollbackPost,
} from "@/api/generated/update/update";
import type { UpdateJobInfo, UpdateStatusResponse } from "@/api/generated/models";
import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { ProgressBar } from "@/components/ProgressBar";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatCheckedAt, formatReleaseDate } from "@/lib/updateFormat";
import { cn } from "@/lib/utils";

/** How often to re-poll `system/status` while waiting for a post-update restart to finish (mirrors PowerPanel). */
const DEFAULT_RESTART_POLL_INTERVAL_MS = 4_000;

/** How often to re-poll `update/status` while a job is running or restarting, per the spec. */
const JOB_POLL_INTERVAL_MS = 2_000;

/** A check is considered stale (and auto-fired on mount) once it is this old. */
const STALE_CHECK_SECONDS = 60 * 60;

/**
 * How long the restarting screen waits for the Portal to come back before
 * showing recovery guidance instead of "Restarting…" forever -- covers a
 * process that crash-loops after `restart_portal()` succeeds (failure-mode
 * audit item 5).
 */
const DEFAULT_RESTART_MAX_WAIT_MS = 10 * 60 * 1000;

/**
 * When this browser first observed the restart currently being waited on
 * (keyed by `job.restarting_at`, the job's identity) -- module-scoped, not
 * component state, so it survives an `UpdatePanel` unmount/remount (e.g.
 * navigating to another screen and back).
 *
 * That persistence matters: the *only* other path to the "Palmimo has not
 * come back yet" guidance is the server's own 600s expiry (core/update.py),
 * and that expiry only ever runs inside `GET /update/status` handling and at
 * boot -- it cannot fire on its own while nothing is polling it. In the one
 * scenario this guidance exists for (a crash-loop after `restart_portal()`
 * succeeds, so the fail-then-succeed poll below never observes a
 * transition), the client-side timeout is the *only* path to that guidance.
 * Arming it fresh on every mount would let a user who bounces between
 * screens postpone it indefinitely.
 *
 * Cleared once a poll shows the job has left `restarting` (done/failed/idle
 * -- see the effect below), so a later, unrelated restart starts its own
 * fresh budget even if `restarting_at` happens to collide (e.g. both null,
 * from a job predating this field).
 */
let restartObservation: { key: number | null; observedAtMs: number } | null = null;

/** Test-only: module state above is otherwise process-lifetime, so tests must reset it between cases. */
export function __resetRestartObservationForTests() {
  restartObservation = null;
}

/** Test-only: exposes the current observation so a test can assert it was rekeyed on a new restart, not just infer it from timing. */
export function __getRestartObservationForTests() {
  return restartObservation;
}

type DialogKind = "update" | "rollback" | null;

/**
 * Resolves an `Updater.apply` step name (see palmimo_portal/ports.py's
 * `Updater` protocol) to its translated label via explicit `t("update.step…")`
 * calls -- a table lookup would be invisible to the i18n-parity scan
 * (`test_i18n_parity.py`), which only recognizes literal `t(...)` calls; see
 * `src/lib/navLabels.ts` for the same pattern. Falls back to the raw step
 * name for any value the backend has not yet been taught.
 */
function stepLabel(t: TFunction, step: string | null): string {
  switch (step) {
    case "fetch":
      return t("update.stepFetch");
    case "assets":
      return t("update.stepAssets");
    case "checkout":
      return t("update.stepCheckout");
    case "sync":
      return t("update.stepSync");
    case "install-assets":
      return t("update.stepInstallAssets");
    case "restart":
      return t("update.stepRestart");
    case "start":
      return t("update.stepStart");
    default:
      return step ?? "";
  }
}

/** A 429 rate limit is not actionable, so it stays silent (failure-mode audit item 8). */
function isSilentRateLimit(error: unknown): boolean {
  return error instanceof PortalApiError && error.status === 429;
}

/**
 * The update screen's logic (see routes/update.tsx, which wraps this in
 * `AppShell`). Free of router hooks -- `onRestarted` is the only reach-out to
 * routing/reloading, so it can be rendered directly with
 * `renderWithProviders(<UpdatePanel onRestarted={...} />)`; the restart-poll
 * interval and `onRestarted` are both injectable for tests.
 */
export function UpdatePanel({
  onRestarted = () => window.location.reload(),
  restartPollIntervalMs = DEFAULT_RESTART_POLL_INTERVAL_MS,
  restartMaxWaitMs = DEFAULT_RESTART_MAX_WAIT_MS,
}: {
  /** Called once a post-update restart is detected complete. The route's default reloads the page so the new bundle loads. */
  onRestarted?: () => void;
  /** Injectable so tests do not have to wait out the real interval. */
  restartPollIntervalMs?: number;
  /** Injectable so tests do not have to wait out the real 10-minute default. */
  restartMaxWaitMs?: number;
}) {
  const [dialogKind, setDialogKind] = useState<DialogKind>(null);
  const [restartTimedOut, setRestartTimedOut] = useState(false);
  const hasAutoCheckedRef = useRef(false);
  const queryClient = useQueryClient();
  const statusQueryKey = getGetStatusApiV1UpdateStatusGetQueryKey();

  // Every check/apply/rollback response is a fresh UpdateStatusResponse --
  // write it straight into the status query's cache instead of waiting for
  // the next poll, so a job that just moved to "running"/"restarting"
  // is visible immediately and the conditional `refetchInterval` below can
  // kick in. `invalidateQueries` with `refetchType: "none"` then marks the
  // entry stale for the next natural refetch, without forcing an immediate
  // one -- an eager refetch could race this write with a GET that has not
  // caught up yet and clobber it with a stale read.
  function adoptStatus(data: UpdateStatusResponse) {
    queryClient.setQueryData(statusQueryKey, data);
    void queryClient.invalidateQueries({ queryKey: statusQueryKey, refetchType: "none" });
  }

  const { data: status, error: statusError } = useGetStatusApiV1UpdateStatusGet({
    query: {
      refetchInterval: (query) => {
        const jobState = query.state.data?.job.state;
        return jobState === "running" || jobState === "restarting" ? JOB_POLL_INTERVAL_MS : false;
      },
    },
  });

  const check = useCheckApiV1UpdateCheckPost({
    mutation: {
      onSuccess: adoptStatus,
    },
  });
  const apply = useApplyApiV1UpdateApplyPost({
    mutation: {
      onSuccess: (data) => {
        adoptStatus(data);
        setDialogKind(null);
      },
      onError: () => setDialogKind(null),
    },
  });
  const rollback = useRollbackApiV1UpdateRollbackPost({
    mutation: {
      onSuccess: (data) => {
        adoptStatus(data);
        setDialogKind(null);
      },
      onError: () => setDialogKind(null),
    },
  });

  // Auto-check once on mount when the last check is missing or stale; never
  // again even if this first check fails or is rate-limited.
  useEffect(() => {
    if (hasAutoCheckedRef.current || status === undefined) {
      return;
    }
    hasAutoCheckedRef.current = true;
    const stale = status.checked_at === null || Date.now() / 1000 - status.checked_at > STALE_CHECK_SECONDS;
    if (stale) {
      check.mutate(undefined, { onError: () => undefined });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const job = status?.job;

  // Poll system/status once the job is restarting, like PowerPanel's reboot
  // detection: a poll only counts once it has failed (Portal actually went
  // down) and then succeeded again. See PowerPanel.tsx for the rationale.
  const onRestartedRef = useRef(onRestarted);
  onRestartedRef.current = onRestarted;
  const hasFailedRef = useRef(false);

  // A restart faster than one `restartPollIntervalMs` tick could settle
  // (`job.state === "done"` with `installed.tag === job.target`) before the
  // poll below ever observes a failed-then-succeeded transition. Tracks
  // whether the *previous* `status` was still `"restarting"`, so this fires
  // once, right at that transition, not on every later "done" render.
  const wasRestartingRef = useRef(false);
  useEffect(() => {
    if (job?.state === "restarting") {
      wasRestartingRef.current = true;
      return;
    }
    if (wasRestartingRef.current && job?.state === "done" && status?.installed.tag === job.target) {
      wasRestartingRef.current = false;
      onRestartedRef.current();
    } else {
      wasRestartingRef.current = false;
    }
  }, [job?.state, job?.target, status?.installed.tag]);

  useEffect(() => {
    if (job?.state !== "restarting") {
      return;
    }
    let cancelled = false;
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    hasFailedRef.current = false;

    async function poll() {
      if (cancelled) return;
      try {
        await getStatusApiV1SystemStatusGet({ signal: AbortSignal.timeout(restartPollIntervalMs) });
        if (cancelled) return;
        if (hasFailedRef.current) {
          cancelled = true;
          onRestartedRef.current();
          return;
        }
      } catch {
        if (!cancelled) {
          hasFailedRef.current = true;
        }
      }
      if (!cancelled) {
        timeoutId = setTimeout(() => void poll(), restartPollIntervalMs);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timeoutId);
    };
  }, [job?.state, restartPollIntervalMs]);

  // The restart-wait timeout: covers a crash-loop after restart_portal()
  // succeeds, where the poll above never observes a fail-then-succeed
  // transition. Starts counting when the job becomes "restarting" and
  // resets when it stops.
  //
  // Anchored to *this browser's* clock, via the module-level
  // `restartObservation` above, rather than to `job.restarting_at` (a
  // server/device epoch *seconds* timestamp set by `mark_restarting`, see
  // core/update.py). The Pi has no RTC: right after boot, before NTP
  // settles, its clock can be minutes off from the browser's, and mixing
  // the two meant a device clock behind the browser fired this guidance
  // immediately on a healthy restart, while a device clock ahead silently
  // extended the wait. The server's own 600s expiry (core/update.py) that
  // flips the job to `failed` is unaffected by this bug or this fix -- it
  // is purely a client-side UI budget, and a server-reported `failed` still
  // wins the moment a poll observes it, since that changes `job.state` and
  // the branch below clears the observation and tears the timer down.
  //
  // The budget -- via `restartObservation`'s key -- only resets when
  // `job.restarting_at` changes (a genuinely new restart superseding the
  // one being waited on), not on every poll that merely re-confirms the
  // same restart, and not on an unmount/remount of this component: while
  // `job === undefined` (status not yet loaded, e.g. right after a
  // remount), this effect deliberately leaves `restartObservation`
  // untouched rather than treating "unknown" as "not restarting".
  useEffect(() => {
    if (job === undefined) {
      return undefined;
    }
    if (job.state !== "restarting") {
      restartObservation = null;
      setRestartTimedOut(false);
      return undefined;
    }
    const key = job.restarting_at ?? null;
    if (restartObservation === null || restartObservation.key !== key) {
      restartObservation = { key, observedAtMs: Date.now() };
      // A stale `restartTimedOut` from a *previous* restart must not bleed
      // into this new one -- e.g. two restarting polls back to back with no
      // intermediate non-restarting state in between (a missed transition)
      // would otherwise re-arm the timer below while still rendering the
      // old restart's power-cycle guidance for however long the old
      // `restartTimedOut === true` render lingers. `restartTimedOut` is
      // therefore reset in lockstep with rekeying `restartObservation`
      // above, not just when leaving "restarting" entirely.
      setRestartTimedOut(false);
    }
    const remainingMs = restartObservation.observedAtMs + restartMaxWaitMs - Date.now();
    if (remainingMs <= 0) {
      setRestartTimedOut(true);
      return undefined;
    }
    const timeoutId = setTimeout(() => setRestartTimedOut(true), remainingMs);
    return () => clearTimeout(timeoutId);
    // `job` itself is deliberately not a dep below: it is a fresh object on
    // every poll even when `state`/`restarting_at` (the only fields this
    // effect reads) are unchanged, and re-running on every poll would
    // needlessly re-arm the timer each time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.state, job?.restarting_at, restartMaxWaitMs]);

  if (!status) {
    return <ApiErrorAlert error={statusError} />;
  }

  return (
    <div className="flex flex-col gap-6 md:flex-row md:items-start">
      <div className="flex flex-1 flex-col gap-6">
        <UpdateCard
          status={status}
          job={job as UpdateJobInfo}
          dialogKind={dialogKind}
          setDialogKind={setDialogKind}
          checkPending={check.isPending}
          checkError={check.error}
          onCheckNow={() => check.mutate(undefined, { onError: () => undefined })}
          onConfirmUpdate={() => status.latest && apply.mutate({ data: { tag: status.latest.tag } })}
          onRetry={() => job?.target && apply.mutate({ data: { tag: job.target } })}
          applyPending={apply.isPending}
          applyError={apply.error}
          restartTimedOut={restartTimedOut}
        />
        {/* previous_tag can coincide with installed.tag after a retry
            (core/update.py's start_apply()/start_rollback() keep the
            existing previous_tag unchanged); hide the card rather than
            show a no-op rollback. */}
        {status.previous_tag && status.previous_tag !== status.installed.tag ? (
          <RollbackCard
            previousTag={status.previous_tag}
            dialogKind={dialogKind}
            setDialogKind={setDialogKind}
            onConfirmRollback={() => rollback.mutate(undefined)}
            rollbackPending={rollback.isPending}
            rollbackError={rollback.error}
          />
        ) : null}
      </div>
    </div>
  );
}

function UpdateCard({
  status,
  job,
  dialogKind,
  setDialogKind,
  checkPending,
  checkError,
  onCheckNow,
  onConfirmUpdate,
  onRetry,
  applyPending,
  applyError,
  restartTimedOut,
}: {
  status: UpdateStatusResponse;
  job: UpdateJobInfo;
  dialogKind: DialogKind;
  setDialogKind: (kind: DialogKind) => void;
  checkPending: boolean;
  checkError: unknown;
  onCheckNow: () => void;
  onConfirmUpdate: () => void;
  onRetry: () => void;
  applyPending: boolean;
  applyError: unknown;
  restartTimedOut: boolean;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <Download className="size-5 text-muted-foreground" aria-hidden />
        <p className="font-semibold">
          {status.update_available
            ? t("update.newVersionAvailable")
            : status.latest === null
              ? t("update.checkFailedTitle")
              : t("update.upToDate")}
        </p>
        {status.latest ? (
          <Badge variant="outline" className="ml-auto">
            {status.latest.tag}
          </Badge>
        ) : null}
      </div>
      <div className="border-t border-border" />
      <dl className="flex flex-col gap-2 text-sm">
        <KvRow label={t("update.installedVersionLabel")} value={status.installed.tag ?? status.installed.commit} />
        <KvRow
          label={t("update.latestReleaseLabel")}
          value={status.latest ? `${status.latest.tag} (${formatReleaseDate(status.latest.published_at)})` : null}
        />
        <KvRow label={t("update.lastCheckedLabel")} value={formatCheckedAt(status.checked_at)} />
      </dl>

      {job.state === "running" ? (
        <div className="flex flex-col gap-2 rounded-lg bg-muted p-3">
          <p className="text-sm">{t("update.progressLabel", { step: stepLabel(t, job.step) })}</p>
          <ProgressBar label={t("common.inProgress")} />
        </div>
      ) : job.state === "restarting" && restartTimedOut ? (
        <div className="flex flex-col gap-2">
          <Alert variant="destructive">
            <AlertDescription>{t("update.restartTimedOut")}</AlertDescription>
          </Alert>
          <Button variant="outline" className="w-fit" onClick={() => window.location.reload()}>
            {t("update.reopenPortalButton")}
          </Button>
        </div>
      ) : job.state === "restarting" ? (
        <div className="flex flex-col gap-2 rounded-lg bg-muted p-3">
          <p className="text-sm font-medium">{t("update.restartingTitle")}</p>
          <p className="text-sm text-muted-foreground">{t("update.restartingBody")}</p>
          <ProgressBar label={t("common.inProgress")} />
        </div>
      ) : job.state === "failed" ? (
        <div className="flex flex-col gap-2">
          <Alert variant="destructive">
            <AlertDescription>
              {t("update.failedTitle")}
              {job.step ? ` (${stepLabel(t, job.step)})` : ""}
              {job.error ? `: ${job.error}` : ""}
            </AlertDescription>
          </Alert>
          {status.retry_available ? (
            <Button variant="outline" className="w-fit" onClick={onRetry} disabled={applyPending}>
              {t("update.retryButton")}
            </Button>
          ) : null}
        </div>
      ) : job.state === "done" ? (
        <Alert>
          <AlertDescription>{t("update.doneMessage", { target: job.target ?? "" })}</AlertDescription>
        </Alert>
      ) : null}

      <p className="text-xs text-muted-foreground">{t("update.notes")}</p>
      {/* A manual "Check now" failure must be visible (failure-mode audit
          item 8); 429 stays silent, matching the auto-check-on-mount path. */}
      <ApiErrorAlert error={isSilentRateLimit(checkError) ? undefined : checkError} />
      <ApiErrorAlert error={applyError} />

      <div className="flex flex-col gap-3 md:flex-row">
        <Button
          className={cn("flex-1 md:flex-initial")}
          disabled={
            (!status.update_available && !status.retry_available) ||
            !status.latest ||
            applyPending ||
            job.state === "running" ||
            job.state === "restarting"
          }
          onClick={() => setDialogKind("update")}
        >
          {status.latest ? t("update.updateButton", { tag: status.latest.tag }) : t("update.updateButton", { tag: "" })}
        </Button>
        {status.latest ? (
          <Button variant="outline" asChild>
            <a href={status.latest.html_url} target="_blank" rel="noreferrer">
              {t("update.releaseNotesButton")}
            </a>
          </Button>
        ) : null}
        <Button variant="ghost" onClick={onCheckNow} disabled={checkPending}>
          {t("update.checkNowButton")}
        </Button>
      </div>

      <AlertDialog open={dialogKind === "update"} onOpenChange={(open) => !open && setDialogKind(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("update.updateDialogTitle", { tag: status.latest?.tag ?? "" })}</AlertDialogTitle>
            <AlertDialogDescription>{t("update.notes")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={applyPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button disabled={applyPending} onClick={onConfirmUpdate}>
              {t("update.updateDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function RollbackCard({
  previousTag,
  dialogKind,
  setDialogKind,
  onConfirmRollback,
  rollbackPending,
  rollbackError,
}: {
  previousTag: string;
  dialogKind: DialogKind;
  setDialogKind: (kind: DialogKind) => void;
  onConfirmRollback: () => void;
  rollbackPending: boolean;
  rollbackError: unknown;
}) {
  const { t } = useTranslation();

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-border bg-card p-4">
      <p className="font-semibold">{t("update.rollbackTitle")}</p>
      <dl className="flex flex-col gap-2 text-sm">
        <KvRow label={t("update.previousVersionLabel")} value={previousTag} />
      </dl>
      <p className="text-xs text-muted-foreground">{t("update.rollbackNote")}</p>
      <ApiErrorAlert error={rollbackError} />
      <Button variant="outline" className="w-fit" disabled={rollbackPending} onClick={() => setDialogKind("rollback")}>
        {t("update.rollbackButton", { tag: previousTag })}
      </Button>

      <AlertDialog open={dialogKind === "rollback"} onOpenChange={(open) => !open && setDialogKind(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("update.rollbackDialogTitle", { tag: previousTag })}</AlertDialogTitle>
            <AlertDialogDescription>{t("update.rollbackNote")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rollbackPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button disabled={rollbackPending} onClick={onConfirmRollback}>
              {t("update.rollbackDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function KvRow({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium">{value ?? "—"}</dd>
    </div>
  );
}
