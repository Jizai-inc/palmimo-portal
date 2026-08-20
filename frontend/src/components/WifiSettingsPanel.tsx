import { useQueryClient } from "@tanstack/react-query";
import { Info, Wifi, WifiOff } from "lucide-react";
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
import { cn } from "@/lib/utils";
import { formatLastWifiAttempt } from "@/lib/wifiAttemptFormat";

type DialogKind = "reconnect" | "forget" | null;

/**
 * The Wi-Fi settings screen's logic (see routes/wifi-settings.tsx, which wraps this in
 * `AppShell`). Free of router hooks -- `onReconnect` is the only reach-out to routing.
 */
export function WifiSettingsPanel({ onReconnect }: { onReconnect: () => void }) {
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

  return (
    <div className="flex flex-col gap-6 md:flex-row md:items-start">
      <div className="flex flex-1 flex-col gap-4 rounded-xl border border-border bg-card p-4">
        <div className="flex items-center gap-2">
          <span
            className={cn("size-2.5 shrink-0 rounded-full", connected ? "bg-green-500" : "bg-muted-foreground/40")}
            aria-hidden
          />
          <span className="font-semibold">
            {connected ? t("wifiSettings.connectedLabel") : t("wifiSettings.disconnectedLabel")}
          </span>
          <Wifi className="ml-auto size-4 text-muted-foreground" aria-hidden />
        </div>
        <div className="border-t border-border" />
        <dl className="flex flex-col gap-2 text-sm">
          <KvRow label={t("wifiSettings.networkLabel")} value={wifiStatus?.ssid} />
          <KvRow label={t("wifiSettings.ipAddressLabel")} value={wifiStatus?.ip_address} mono />
          <KvRow label={t("wifiSettings.hostnameLabel")} value={systemStatus?.hostname} />
          <KvRow
            label={t("wifiSettings.lastAttemptLabel")}
            value={formatLastWifiAttempt(systemStatus?.last_wifi_attempt, t)}
          />
        </dl>
        <div className="border-t border-border" />
        <ApiErrorAlert error={forget.error} />
        <div className="flex flex-col gap-3 md:flex-row">
          <Button variant="outline" className="flex-1" onClick={() => setDialogKind("reconnect")}>
            {t("wifiSettings.connectAnother")}
          </Button>
          <Button
            variant="destructive"
            className="flex-1"
            onClick={() => setDialogKind("forget")}
            disabled={!hostnameKnown}
          >
            {hostnameKnown ? t("wifiSettings.forgetNetwork") : t("common.loading")}
          </Button>
        </div>
      </div>

      <div className="flex w-full flex-col gap-2 rounded-xl border border-border bg-card p-4 md:w-[320px] md:shrink-0">
        <div className="flex items-center gap-2">
          <Info className="size-4 text-muted-foreground" aria-hidden />
          <p className="font-semibold">{t("wifiSettings.noteTitle")}</p>
        </div>
        <p className="text-sm text-muted-foreground">{t("wifiSettings.noteBody", { ssid, hostname })}</p>
      </div>

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

function KvRow({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={cn("font-medium", mono ? "font-mono" : undefined)}>{value ?? "—"}</dd>
    </div>
  );
}
