import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getStatusApiV1SystemStatusGet, useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import type { SystemStatus } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AuthShell } from "@/components/AuthShell";
import { ProgressBar } from "@/components/ProgressBar";
import { Button } from "@/components/ui/button";
import { startPortalProbe } from "@/lib/portalProbe";

/** How often to re-poll `system/status` while waiting for the AP to come back (or not). */
const DEFAULT_POLL_INTERVAL_MS = 4_000;

/**
 * How long this screen waits before giving up and showing recovery guidance
 * instead of spinning forever -- covers comitup being down at the
 * pre-connect read, or any other reason the same-origin poll never resolves.
 */
const DEFAULT_MAX_WAIT_MS = 5 * 60 * 1000;

type PollOutcome = { kind: "waiting" } | { kind: "connected"; status: SystemStatus } | { kind: "failed"; status: SystemStatus };

/**
 * The wifi-waiting screen's logic (routes/wifi.waiting.tsx wraps it in a `<div>`). No router
 * hooks: `onConnected`/`onFailed` are the only reach-out to routing.
 *
 * Two independent recovery paths race each other, both tearing down on unmount and stopping
 * as soon as one resolves the screen:
 *
 * 1. **Same-origin polling** of `system/status`, swallowing failures since the AP being down
 *    is expected mid-wait. Resolves "failed" on `last_wifi_attempt.result === "failed"`, and
 *    "connected" on `state === "connected"` AND `last_wifi_attempt` absent or itself
 *    `"connected"` -- the attempt check guards the reconfigure race (core/wifi_attempt.py),
 *    where the backend can briefly still observe `CONNECTED` to the *old* network.
 * 2. **The portal probe** (src/lib/portalProbe.ts): an opaque `no-cors` fetch to
 *    `http://<hostname>.local/` every few seconds, for a visitor device that rejoined its
 *    home Wi-Fi. First success triggers `assign`.
 */
export function WifiWaitingPanel({
  ssid,
  onConnected,
  onFailed,
  previousAttemptTimestamp = 0,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  probeIntervalMs,
  assign = (url: string) => window.location.assign(url),
  maxWaitMs = DEFAULT_MAX_WAIT_MS,
  connectError,
}: {
  ssid: string;
  /** Called once the same-origin poll observes `state === "connected"`. */
  onConnected: () => void;
  /** Called on a failed attempt, or from the timed-out/pre-attempt-error screens' Back/Try-again actions. */
  onFailed: () => void;
  /**
   * The `last_wifi_attempt.timestamp` on record at submit (routes/wifi.tsx's `since` search
   * param). A polled `last_wifi_attempt` is trusted as describing *this* attempt only once its
   * `ssid` matches and its `timestamp` exceeds this value (both server-side, no clock-skew
   * risk). Defaults to `0` so an untouched status never looks "in the future".
   */
  previousAttemptTimestamp?: number;
  /** Injectable so tests do not have to wait out the real interval. */
  pollIntervalMs?: number;
  probeIntervalMs?: number;
  /** Injectable so tests do not trigger a real jsdom navigation. */
  assign?: (url: string) => void;
  /** Injectable so tests do not have to wait out the real 5-minute default. */
  maxWaitMs?: number;
  /**
   * The error from the `connect` mutation, if the POST itself failed before any attempt was
   * recorded for the same-origin poll to find -- without this, spins as "waiting" until
   * `maxWaitMs` instead of showing the real error.
   */
  connectError?: unknown;
}) {
  const { t } = useTranslation();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const [outcome, setOutcome] = useState<PollOutcome>({ kind: "waiting" });
  const outcomeRef = useRef(outcome);
  outcomeRef.current = outcome;
  const [probeFound, setProbeFound] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const timedOutRef = useRef(false);
  timedOutRef.current = timedOut;

  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;
  const onFailedRef = useRef(onFailed);
  onFailedRef.current = onFailed;

  // Read through a ref so the probe effect below does not restart on every fresh default
  // arrow-function identity.
  const assignRef = useRef(assign);
  assignRef.current = assign;

  const hostname = status?.hostname;
  const urlHostname = hostname ?? "palmimo";

  // Fires once, `maxWaitMs` after mount, unless already resolved.
  useEffect(() => {
    if (outcomeRef.current.kind !== "waiting") return undefined;
    const timeoutId = setTimeout(() => {
      if (outcomeRef.current.kind === "waiting") {
        setTimedOut(true);
      }
    }, maxWaitMs);
    return () => clearTimeout(timeoutId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maxWaitMs]);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      if (cancelled || outcomeRef.current.kind !== "waiting" || timedOutRef.current) return;
      try {
        const polledStatus = await getStatusApiV1SystemStatusGet();
        if (cancelled) return;
        const attempt = polledStatus.last_wifi_attempt;
        // Authoritative for *this* attempt only once it names this ssid and postdates
        // `previousAttemptTimestamp` -- otherwise a stale record. A missing attempt is
        // vacuously trusted.
        const isThisAttempt = attempt == null || (attempt.ssid === ssid && attempt.timestamp > previousAttemptTimestamp);
        if (!isThisAttempt) {
          // Keep waiting -- the poll has not yet caught up.
        } else if (polledStatus.state === "connected" && (attempt == null || attempt.result === "connected")) {
          setOutcome({ kind: "connected", status: polledStatus });
        } else if (attempt?.result === "failed") {
          setOutcome({ kind: "failed", status: polledStatus });
        }
        // Anything else stays "waiting", including `state === "connected"`
        // with an attempt still `"attempting"` (the reconfigure race).
      } catch {
        // Ordinarily a fetch-level TypeError while the AP is mid-teardown -- the expected
        // symptom of the connect succeeding, not a real failure. Try again next tick.
      }
    }

    const interval = setInterval(() => void poll(), pollIntervalMs);
    void poll();
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
    // `timedOut` is included so the interval is actually torn down the moment max-wait fires.
  }, [pollIntervalMs, ssid, previousAttemptTimestamp, timedOut]);

  // The probe must never call `assign` after the poll has resolved the screen (it would yank
  // the browser out from under a navigation in progress): `onFound` re-checks `outcomeRef`.
  const isWaiting = outcome.kind === "waiting" && !timedOut;
  useEffect(() => {
    if (!hostname || !isWaiting) return undefined;
    const stop = startPortalProbe(
      hostname,
      () => {
        if (outcomeRef.current.kind !== "waiting") return;
        setProbeFound(true);
        assignRef.current(`http://${hostname}.local/`);
      },
      probeIntervalMs,
    );
    return stop;
  }, [hostname, probeIntervalMs, isWaiting]);

  if (outcome.kind === "connected") {
    return (
      <AuthShell title={t("wifi.waitingSuccessTitle")}>
        <p className="text-sm text-muted-foreground">{t("wifi.waitingSuccessBody", { ssid })}</p>
        <Button className="w-full" onClick={onConnectedRef.current}>
          {t("wifi.goToDashboard")}
        </Button>
      </AuthShell>
    );
  }

  if (outcome.kind === "failed") {
    const observed = outcome.status.last_wifi_attempt?.observed_connection_name;
    // comitup settled on a *different* known network: name it instead of the generic guidance.
    // Requires `state === "connected"`; resolve_attempt already nulls this otherwise -- defense in depth.
    const joinedDifferentNetwork = outcome.status.state === "connected" && observed != null && observed !== ssid;
    return (
      <AuthShell title={t("wifi.waitingFailedTitle")}>
        <p className="text-sm text-muted-foreground">
          {joinedDifferentNetwork
            ? t("wifi.lastAttemptFailedDifferentNetwork", { ssid, observed })
            : t("wifi.lastAttemptFailed", { ssid: outcome.status.last_wifi_attempt?.ssid ?? ssid })}
        </p>
        <Button className="w-full" onClick={onFailedRef.current}>
          {t("wifi.tryAgain")}
        </Button>
      </AuthShell>
    );
  }

  if (connectError && outcome.kind === "waiting") {
    // Surface the real error immediately instead of spinning until maxWaitMs.
    return (
      <AuthShell title={t("wifi.waitingFailedTitle")}>
        <ApiErrorAlert error={connectError} />
        <Button className="w-full" variant="outline" onClick={onFailedRef.current}>
          {t("common.back")}
        </Button>
      </AuthShell>
    );
  }

  if (timedOut) {
    return (
      <AuthShell title={t("wifi.waitingTimedOutTitle")}>
        <p className="text-sm text-muted-foreground">
          {t("wifi.waitingTimedOut", { hostname: hostname ?? t("wifi.hostnameUnknown") })}
        </p>
        <div className="flex gap-3">
          <Button variant="outline" className="flex-1" onClick={onFailedRef.current}>
            {t("common.back")}
          </Button>
          <Button className="flex-1" onClick={onFailedRef.current}>
            {t("wifi.tryAgain")}
          </Button>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell title={t("wifi.waitingTitle")}>
      <div className="flex flex-col gap-4">
        <p className="text-sm text-muted-foreground">
          {t("wifi.waitingBody", { ssid, hostname: hostname ?? t("wifi.hostnameUnknown") })}
        </p>
        <ProgressBar label={t("common.inProgress")} />
        <div className="flex flex-col gap-3 rounded-md bg-muted p-3 text-sm">
          <NumberedStep n={1} text={t("wifi.waitingStep1", { ssid })} />
          <NumberedStep n={2} text={t("wifi.waitingStep2")} />
          <div className="flex items-center justify-between gap-2 rounded-md border border-input bg-background px-3 py-2">
            <a href={`http://${urlHostname}.local`} className="truncate text-sm underline underline-offset-2">
              {`http://${urlHostname}.local`}
            </a>
            <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
              {probeFound ? (
                t("wifi.waitingFound")
              ) : (
                <>
                  <LoaderCircle className="size-3.5 animate-spin" />
                  {t("wifi.waitingProbing")}
                </>
              )}
            </span>
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          {t("wifi.waitingFailureNote", { hostname: hostname ?? t("wifi.hostnameUnknown") })}
        </p>
        {/* Labeled distinctly (wifi.waitingBackToScan, not common.back): navigating away here
          does NOT cancel the connect attempt already in flight on the device. */}
        <Button variant="outline" className="w-full" onClick={onFailedRef.current}>
          {t("wifi.waitingBackToScan")}
        </Button>
      </div>
    </AuthShell>
  );
}

function NumberedStep({ n, text }: { n: number; text: string }) {
  return (
    <div className="flex items-start gap-2.5">
      <span className="flex size-[22px] shrink-0 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">
        {n}
      </span>
      <p>{text}</p>
    </div>
  );
}
