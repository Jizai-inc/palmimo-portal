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
  useListSavedApiV1WifiSavedGet,
  useSaveNetworkApiV1WifiSavedPost,
} from "@/api/generated/wifi/wifi";
import { getGetStatusApiV1SystemStatusGetQueryKey, useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import type { WifiNetworkResponse } from "@/api/generated/models";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { AuthShell } from "@/components/AuthShell";
import { Button } from "@/components/ui/button";
import { WifiConnectForm } from "@/components/WifiConnectForm";
import { signalIcon } from "@/lib/wifiSignal";

interface WifiSearch {
  /** `?reconfigure=1`: entered from `/wifi-settings`'s "connect to another network" action, not the unprovisioned setup flow. Changes the copy shown and where "back" navigates. */
  reconfigure?: boolean;
  /** `?add=1`: entered from `/wifi-settings`'s "add" action -- picking a network saves it without switching. */
  add?: boolean;
}

/**
 * Wi-Fi scan/connect flow: scan -> pick a network -> enter password -> submit. Submitting
 * navigates straight to `/wifi/waiting` without awaiting a result, since the connect attempt
 * tears down the AP the browser is talking through (AP-disconnection-asymmetry).
 */
export const Route = createFileRoute("/wifi")({
  validateSearch: (search: Record<string, unknown>): WifiSearch => ({
    reconfigure: search.reconfigure === "1" || search.reconfigure === true,
    add: search.add === "1" || search.add === true,
  }),
  component: WifiScreen,
});

function WifiScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { reconfigure, add } = Route.useSearch();
  const fromSettings = Boolean(reconfigure || add);
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
  const connect = useConnectApiV1WifiConnectPost();
  const { data: saved } = useListSavedApiV1WifiSavedGet({ query: { enabled: fromSettings } });
  const save = useSaveNetworkApiV1WifiSavedPost({
    mutation: { onSuccess: () => void navigate({ to: "/wifi-settings" }) },
  });
  const savedState = (ssid: string) => saved?.find((item) => item.ssid === ssid);

  const lastAttempt = status?.last_wifi_attempt;
  const goToWifiSettings = () => void navigate({ to: "/wifi-settings" });

  function handleConnectSubmit(psk: string) {
    if (!selected) return;
    const ssid = selected.ssid;
    // Read here (pre-submit) so WifiWaitingPanel's poll can tell this attempt apart from a
    // still-cached one -- see its `previousAttemptTimestamp` prop.
    const since = status?.last_wifi_attempt?.timestamp ?? 0;
    // Captured before `.mutate()`, so it precedes the mutation's own `submittedAt`.
    const submitted = Date.now();
    // Fired, not awaited: the connect attempt tears down this AP, so the request can fail with
    // a network-level error even on success -- that failure is expected (AP-disconnection-
    // asymmetry; routes/wifi.waiting.tsx handles it).
    connect.mutate({ data: { ssid, psk } });
    // Navigate immediately after firing, before any other work -- these two calls must not sit
    // between "fire" and "transition" (issue #13: the AP teardown can start before this ever
    // yields, so anything here delays the only visible feedback the user gets).
    void navigate({ to: "/wifi/waiting", search: { ssid, since, submitted } });
    // Cache hygiene only -- WifiWaitingPanel's poll detects the outcome.
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
    void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1WifiStatusGetQueryKey() });
  }

  if (selected && add) {
    const already = Boolean(savedState(selected.ssid));
    return (
      <AuthShell
        title={already ? t("wifi.updateTitle", { ssid: selected.ssid }) : t("wifi.saveTitle", { ssid: selected.ssid })}
        description={already ? t("wifi.updateBody") : t("wifi.saveBody")}
      >
        <WifiConnectForm
          network={selected}
          lastAttempt={null}
          connectError={save.error}
          isSubmitting={save.isPending}
          onSubmit={(psk) => save.mutate({ data: { ssid: selected.ssid, psk } })}
          onBack={() => setSelected(null)}
          submitLabel={t("wifi.save")}
          submittingLabel={t("wifi.saving")}
        />
      </AuthShell>
    );
  }

  if (selected) {
    return (
      <AuthShell title={t("wifi.connectTitle", { ssid: selected.ssid })} description={t("wifi.connectBody")}>
        <WifiConnectForm
          network={selected}
          lastAttempt={lastAttempt}
          connectError={connect.error}
          isSubmitting={connect.isPending}
          onSubmit={handleConnectSubmit}
          onBack={() => (reconfigure ? goToWifiSettings() : setSelected(null))}
        />
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title={add ? t("wifi.addTitle") : t("wifi.scanTitle")}
      description={add ? t("wifi.addBody") : t("wifi.scanBody")}
    >
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
                const savedEntry = fromSettings ? savedState(network.ssid) : undefined;
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
                      {savedEntry ? (
                        <span
                          className={
                            savedEntry.active
                              ? "shrink-0 rounded-md bg-green-500/15 px-2 py-0.5 text-xs text-green-700 dark:text-green-400"
                              : "shrink-0 rounded-md bg-secondary px-2 py-0.5 text-xs text-secondary-foreground"
                          }
                        >
                          {savedEntry.active ? t("wifi.connectedBadge") : t("wifi.savedBadge")}
                        </span>
                      ) : null}
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
        {fromSettings ? (
          <Button variant="ghost" className="w-full" onClick={goToWifiSettings}>
            {t("common.back")}
          </Button>
        ) : null}
      </div>
    </AuthShell>
  );
}
