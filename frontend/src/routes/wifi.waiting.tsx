import { useMutationState } from "@tanstack/react-query";
import { createFileRoute, redirect, useNavigate } from "@tanstack/react-router";

import { WifiWaitingPanel } from "@/components/WifiWaitingPanel";
import type { ConnectMutationSnapshot } from "@/lib/selectConnectError";
import { selectConnectError } from "@/lib/selectConnectError";
import { shouldRedirectToWifiScan } from "@/lib/wifiWaitingGate";

/**
 * AP-teardown waiting screen, shown the instant `/wifi`'s connect form submits, before any
 * result is known (see palmimo-portal-technical.md, AP-disconnection-asymmetry). Route
 * definition only; logic lives in `WifiWaitingPanel` (no router hooks), unit-tested directly.
 */
interface WifiWaitingSearch {
  ssid: string;
  /** `last_wifi_attempt.timestamp` on record at submit -- see `WifiWaitingPanel`'s `previousAttemptTimestamp` prop. */
  since: number;
  /** `Date.now()` at submit -- this attempt's marker for `selectConnectError`. Defaults to `0` so a malformed param never hides a real error. */
  submitted: number;
}

export const Route = createFileRoute("/wifi/waiting")({
  validateSearch: (search: Record<string, unknown>): WifiWaitingSearch => ({
    ssid: typeof search.ssid === "string" ? search.ssid : "",
    since: typeof search.since === "number" ? search.since : 0,
    submitted: typeof search.submitted === "number" ? search.submitted : 0,
  }),
  // Guards at the route level instead of flashing the wrong screen -- see shouldRedirectToWifiScan.
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
  // The connect POST is fired-and-not-awaited from routes/wifi.tsx, so its result is read back
  // from TanStack Query's mutation cache rather than a local hook here.
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
