import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeftRight, Info, Wifi, WifiOff } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getGetStatusApiV1WifiStatusGetQueryKey,
  useForgetApiV1WifiConnectionDelete,
  useGetStatusApiV1WifiStatusGet,
} from "@/api/generated/wifi/wifi";
import { getGetStatusApiV1SystemStatusGetQueryKey, useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { PortalApiError } from "@/api/client";
import { ApiErrorAlert } from "@/components/ApiErrorAlert";
import { CenteredState } from "@/components/CenteredState";
import { LearnMoreLink } from "@/components/LearnMoreLink";
import { PageHeader } from "@/components/PageHeader";
import { SavedNetworksCard } from "@/components/SavedNetworksCard";
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
import { formatUtcTimestamp } from "@/lib/formatTimestamp";
import { cn } from "@/lib/utils";
import { formatLastWifiAttempt } from "@/lib/wifiAttemptFormat";

type DialogKind = "reconnect" | "forget" | null;

/**
 * The Wi-Fi settings screen's logic (see routes/settings.network.tsx). Free of router hooks -- `onReconnect` is the only reach-out to routing.
 */
export function WifiSettingsPanel({ onReconnect, onAdd }: { onReconnect: () => void; onAdd: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  const [dialogKind, setDialogKind] = useState<DialogKind>(null);
  /**
   * `false` while the settings card is showing; otherwise which terminal state to render.
   * `"network-drop"` is distinct from `"success"`: a forget's DELETE can fail at the network
   * level (a plain `TypeError`, not a parsed {@link PortalApiError}) because the device
   * dropped the LAN mid-response -- expected, not a real failure. Both land on the same
   * terminal screen; only `"network-drop"` adds the extra note.
   */
  const [forgotten, setForgotten] = useState<false | "success" | "network-drop">(false);
  const [switchedTo, setSwitchedTo] = useState<string | null>(null);

  const forget = useForgetApiV1WifiConnectionDelete({
    mutation: {
      onSuccess: () => {
        setDialogKind(null);
        setForgotten("success");
        // No screen after this point may keep rendering the pre-forget status from cache.
        void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() });
        void queryClient.invalidateQueries({ queryKey: getGetStatusApiV1WifiStatusGetQueryKey() });
      },
      onError: (error) => {
        setDialogKind(null);
        if (error instanceof PortalApiError) {
          return; // a real, parsed failure -- stays as an inline error below
        }
        setForgotten("network-drop");
      },
    },
  });

  const connected = wifiStatus?.state === "connected";
  const ssid = wifiStatus?.ssid ?? "";
  const hostname = systemStatus?.hostname ?? "";
  const hostnameKnown = Boolean(hostname);

  if (forgotten) {
    return (
      <CenteredState
        icon={WifiOff}
        title={t("wifiSettings.terminalTitle")}
        body={t("wifiSettings.terminalBody", { hostname })}
        action={
          forgotten === "network-drop" ? (
            <p className="text-xs text-muted-foreground">{t("wifiSettings.terminalDroppedNote")}</p>
          ) : undefined
        }
      />
    );
  }

  if (switchedTo) {
    return (
      <CenteredState
        icon={ArrowLeftRight}
        title={t("wifiSettings.switchingTitle", { ssid: switchedTo })}
        body={t("wifiSettings.switchingBody", { ssid: switchedTo, hostname })}
      />
    );
  }

  const attempt = systemStatus?.last_wifi_attempt;
  const statusLine = !connected
    ? attempt
      ? formatLastWifiAttempt(attempt, t)
      : null
    : attempt?.result === "connected" && attempt.ssid === ssid
      ? t("wifiSettings.connectedSince", { datetime: formatUtcTimestamp(attempt.timestamp, { withYear: false }) })
      : t("wifiSettings.connectedLabel");

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title={t("nav.network")}
        description={t("wifiSettings.subtitle")}
        actions={
          <Button variant="outline" onClick={() => setDialogKind("reconnect")}>
            <ArrowLeftRight className="size-4" aria-hidden />
            {t("wifiSettings.connectAnother")}
          </Button>
        }
      />
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        <div className="flex flex-col gap-4 border-b border-border p-5 sm:flex-row sm:items-center">
          <div className="flex min-w-0 flex-1 items-center gap-3.5">
            <span
              className={cn("flex size-10 shrink-0 items-center justify-center rounded-[10px]", connected ? "bg-success-soft" : "bg-secondary")}
              aria-hidden
            >
              {connected ? <Wifi className="size-5 text-success" /> : <WifiOff className="size-5 text-muted-foreground" />}
            </span>
            <div className="flex min-w-0 flex-col gap-0.5">
              <p className="truncate text-[17px] font-semibold">{connected && ssid ? ssid : t("wifiSettings.disconnectedLabel")}</p>
              {statusLine ? (
                <p className={cn("text-[13px] font-medium", connected ? "text-success" : "text-muted-foreground")}>{statusLine}</p>
              ) : null}
            </div>
          </div>
          <Button variant="dangerOutline" size="sm" className="w-fit" onClick={() => setDialogKind("forget")} disabled={!hostnameKnown}>
            {hostnameKnown ? t("wifiSettings.forgetNetwork") : t("common.loading")}
          </Button>
        </div>
        <dl className="grid grid-cols-1 gap-4 px-5 py-4.5 text-[13px] sm:grid-cols-3">
          <Fact label={t("wifiSettings.ipAddressLabel")} value={wifiStatus?.ip_address} mono />
          <Fact label={t("wifiSettings.hostnameLabel")} value={hostname || null} />
          <Fact label={t("wifiSettings.fallbackHotspotLabel")} value={hostname || null} />
        </dl>
        <p className="flex items-start gap-1.5 bg-subtle px-5 py-3 text-xs text-muted-foreground">
          <Info className="mt-px size-3.5 shrink-0" aria-hidden />
          <span>
            {t("wifiSettings.forgetHint")} <LearnMoreLink />
          </span>
        </p>
      </div>
      <ApiErrorAlert error={forget.error} />
      {connected ? <SavedNetworksCard hostname={hostname} onAdd={onAdd} onSwitched={setSwitchedTo} /> : null}

      <AlertDialog open={dialogKind !== null} onOpenChange={(open) => !open && setDialogKind(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {dialogKind === "reconnect" ? t("wifiSettings.reconnectDialogTitle") : t("wifiSettings.forgetDialogTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {dialogKind === "reconnect"
                ? t("wifiSettings.reconnectDialogBody", { ssid, hostname })
                : t("wifiSettings.noteBody", { ssid, hostname })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={forget.isPending}>{t("common.cancel")}</AlertDialogCancel>
            <Button
              variant={dialogKind === "forget" ? "destructive" : "default"}
              disabled={forget.isPending}
              onClick={() => {
                if (dialogKind === "reconnect") {
                  setDialogKind(null);
                  onReconnect();
                } else {
                  forget.mutate();
                }
              }}
            >
              {dialogKind === "reconnect"
                ? t("wifiSettings.reconnectDialogConfirm")
                : t("wifiSettings.forgetDialogConfirm")}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function Fact({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("truncate", mono && "font-mono")}>{value ?? "—"}</dd>
    </div>
  );
}
