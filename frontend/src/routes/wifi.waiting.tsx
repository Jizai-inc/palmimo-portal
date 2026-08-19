import { useMutationState } from "@tanstack/react-query";
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";

import { WifiWaitingPanel } from "@/components/WifiWaitingPanel";
import type { ConnectMutationSnapshot } from "@/lib/selectConnectError";
import { selectConnectError } from "@/lib/selectConnectError";
import { shouldRedirectToWifiScan } from "@/lib/wifiWaitingGate";

/**
 * AP-teardown waiting screen, shown the instant `/wifi`'s connect form
 * submits, before any result is known (see palmimo-portal-technical.md,
 * AP-disconnection-asymmetry). Route definition only; logic lives in
 * `WifiWaitingPanel` (no router hooks), unit-tested directly -- see
 * components/WifiWaitingPanel.test.tsx.
 */
interface WifiWaitingSearch {
  ssid: string;
  /**
   * `last_wifi_attempt.timestamp` on record when `/wifi`'s connect form
   * submitted -- lets the poll tell this attempt's eventual record apart
   * from a still-cached one. See `WifiWaitingPanel`'s
   * `previousAttemptTimestamp` prop docstring.
   */
  since: number;
  /**
   * `Date.now()` at submit time -- this attempt's marker into TanStack
   * Query's mutation cache. `selectConnectError` matches only entries with
   * `submittedAt >= submitted`. Defaults to `0` (matches anything) so a
   * malformed search param never hides a real error.
   */
  submitted: number;
}

export const Route = createFileRoute("/wifi/waiting")({
  validateSearch: (search: Record<string, unknown>): WifiWaitingSearch => ({
    ssid: typeof search.ssid === "string" ? search.ssid : "",
    since: typeof search.since === "number" ? search.since : 0,
    submitted: typeof search.submitted === "number" ? search.submitted : 0,
  }),
  // `beforeLoad` guards at the route level instead of flashing the wrong
  // screen -- see `shouldRedirectToWifiScan`'s docstring.
  beforeLoad: ({ search }) => {
    if (shouldRedirectToWifiScan(search)) {
      throw redirect({ to: "/wifi" });
    }
  },
  component: WifiWaitingScreen,
});

function WifiWaitingScreen() {
  const navigate = useNavigate();
  const { ssid, since, submitted } = Route.useSearch();
  // The connect POST was fired-and-not-awaited from routes/wifi.tsx, so its
  // result is read back from TanStack Query's mutation cache (keyed by the
  // `mutationKey` orval's `useConnectApiV1WifiConnectPost` registers, see
  // src/api/generated/wifi/wifi.ts) rather than a local hook here.
  // `selectConnectError` filters accumulated retry entries down to the one
  // real `PortalApiError` (not the expected fetch-level `TypeError` from a
  // successful hotspot-to-station AP teardown) matching this exact attempt.
  const connectMutations = useMutationState({
    filters: { mutationKey: ["connectApiV1WifiConnectPost"], status: "error" },
    select: (mutation): ConnectMutationSnapshot => ({
      error: mutation.state.error,
      variables: mutation.state.variables as ConnectMutationSnapshot["variables"],
      submittedAt: mutation.state.submittedAt,
    }),
  });
  const connectError = selectConnectError(connectMutations, ssid, submitted);
  return (
    <WifiWaitingPanel
      ssid={ssid}
      previousAttemptTimestamp={since}
      connectError={connectError}
      onConnected={() => void navigate({ to: "/dashboard" })}
      onFailed={() => void navigate({ to: "/wifi" })}
    />
  );
}
