import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { ChevronRight, Lock } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getGetStatusApiV1WifiStatusGetQueryKey,
  useConnectApiV1WifiConnectPost,
  useGetStatusApiV1WifiStatusGet,
  useListNetworksApiV1WifiNetworksGet,
} from "@/api/generated/wifi/wifi";
import { getGetStatusApiV1SystemStatusGetQueryKey, useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import type { WifiNetworkResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AuthShell } from "@/components/AuthShell";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { signalIcon } from "@/lib/wifiSignal";

interface WifiSearch {
  /**
   * `?reconfigure=1`: entered from `/wifi-settings`'s "connect to another
   * network" action rather than the unprovisioned setup flow. Changes the
   * copy shown and where "back" navigates. Optional so plain-setup-flow
   * callers of `/wifi` and `/wifi/waiting` don't have to pass it.
   */
  reconfigure?: boolean;
}

/**
 * Wi-Fi scan/connect flow: scan -> pick a network -> enter password ->
 * submit. Submitting navigates straight to `/wifi/waiting` without awaiting
 * a result, since the connect attempt tears down the AP the browser is
 * talking through (see palmimo-portal-technical.md, AP-disconnection-asymmetry).
 *
 * Reused by the unprovisioned setup flow and the dashboard's "connect to
 * another network" flow (`?reconfigure=1`, see `WifiSearch.reconfigure`).
 */
export const Route = createFileRoute("/wifi")({
  validateSearch: (search: Record<string, unknown>): WifiSearch => ({
    reconfigure: search.reconfigure === "1" || search.reconfigure === true,
  }),
  component: WifiScreen,
});

function WifiScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { reconfigure } = Route.useSearch();
  const { data: status } = useGetStatusApiV1SystemStatusGet();
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const {
    data: networks,
    isLoading,
    isError: isNetworksError,
    error: networksError,
    refetch,
    isFetching,
  } = useListNetworksApiV1WifiNetworksGet();
  const [selected, setSelected] = useState<WifiNetworkResponse | null>(null);
  const [psk, setPsk] = useState("");
  const connect = useConnectApiV1WifiConnectPost();

  const lastAttempt = status?.last_wifi_attempt;
  const lastAttemptFailed = lastAttempt?.result === "failed";
  const goToWifiSettings = () => void navigate({ to: "/wifi-settings" });

  function handleConnectSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!selected) return;
    const ssid = selected.ssid;
    // `since` (pre-submit `last_wifi_attempt.timestamp`) must be read here so
    // WifiWaitingPanel's poll can tell this attempt apart from a still-cached
    // one -- see its `previousAttemptTimestamp` prop docstring.
    const since = status?.last_wifi_attempt?.timestamp ?? 0;
    // Captured before `.mutate()`, so it precedes the mutation's own
    // `submittedAt` -- see `/wifi/waiting`'s `submitted` search param and
    // `selectConnectError` docstrings.
    const submitted = Date.now();
    // Fired, not awaited: the connect attempt tears down this AP, so the
    // request can fail with a network-level error even on success -- that
    // failure is expected (see palmimo-portal-technical.md,
    // AP-disconnection-asymmetry; routes/wifi.waiting.tsx handles it).
    connect.mutate({ data: { ssid, psk } });
    // Cache hygiene only -- WifiWaitingPanel's poll detects the outcome.
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1WifiStatusGetQueryKey() });
    void navigate({ to: "/wifi/waiting", search: { ssid, since, submitted } });
  }

  if (selected) {
    return (
      <AuthShell title={t("wifi.connectTitle", { ssid: selected.ssid })} description={t("wifi.connectBody")}>
        <form className="flex flex-col gap-4" onSubmit={handleConnectSubmit}>
          {lastAttemptFailed && lastAttempt ? (
            <Alert variant="destructive">
              <AlertTitle>{t("wifi.lastAttemptFailedTitle")}</AlertTitle>
              <AlertDescription>{t("wifi.lastAttemptFailed", { ssid: lastAttempt.ssid })}</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="psk">{t("wifi.passwordLabel")}</Label>
            <Input
              id="psk"
              type="password"
              autoComplete="off"
              required={selected.secured}
              value={psk}
              onChange={(event) => setPsk(event.target.value)}
            />
          </div>
          <ApiErrorAlert error={connect.error} />
          <div className="flex gap-3">
            <Button
              type="button"
              variant="outline"
              className="flex-1"
              onClick={() => (reconfigure ? goToWifiSettings() : setSelected(null))}
            >
              {t("common.back")}
            </Button>
            <Button type="submit" disabled={connect.isPending} className="flex-1">
              {t("wifi.connect")}
            </Button>
          </div>
        </form>
      </AuthShell>
    );
  }

  return (
    <AuthShell title={t("wifi.scanTitle")} description={t("wifi.scanBody")}>
      <div className="flex flex-col gap-4">
        {reconfigure && wifiStatus?.ssid ? (
          <p className="text-xs text-muted-foreground">{t("wifi.reconfigureNote", { ssid: wifiStatus.ssid })}</p>
        ) : null}
        {isNetworksError ? (
          <ApiErrorAlert error={networksError} />
        ) : (
          <>
            <ul className="flex flex-col rounded-md border border-input">
              {(networks ?? []).map((network, index) => {
                const SignalIcon = signalIcon(network.signal);
                return (
                  <li
                    key={`${network.ssid}-${network.secured}-${index}`}
                    className={index === 0 ? "" : "border-t border-input"}
                  >
                    <button
                      type="button"
                      onClick={() => setSelected(network)}
                      className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm hover:bg-secondary"
                    >
                      <SignalIcon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 flex-1 truncate font-medium">{network.ssid}</span>
                      {network.secured ? <Lock className="size-3.5 shrink-0 text-muted-foreground" /> : null}
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {network.secured ? t("wifi.secured") : t("wifi.open")}
                      </span>
                      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                    </button>
                  </li>
                );
              })}
            </ul>
            {!isLoading && (networks ?? []).length === 0 ? (
              <p className="text-sm text-muted-foreground">{t("wifi.noNetworksFound")}</p>
            ) : null}
          </>
        )}
        <Button variant="outline" className="w-full" onClick={() => void refetch()} disabled={isFetching}>
          {t("wifi.rescan")}
        </Button>
        {reconfigure ? (
          <Button variant="ghost" className="w-full" onClick={goToWifiSettings}>
            {t("common.back")}
          </Button>
        ) : null}
      </div>
    </AuthShell>
  );
}
