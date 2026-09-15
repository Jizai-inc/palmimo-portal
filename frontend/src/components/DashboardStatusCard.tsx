import type * as React from "react";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useGetStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** Below this, `dashboard.diskLowWarning` is shown (design doc 3.4's own 1 GB free-space gate). */
const LOW_DISK_BYTES = 1_000_000_000;

/**
 * The dashboard's status card (routes/dashboard.tsx). Fetches its own data so it can be
 * rendered and tested standalone, like SshKeysPanel/PowerPanel. `portalBadge` is an optional
 * slot for the "update available" badge -- passed in rather than built here, since `<Link>`
 * requires a TanStack Router context this component's standalone test harness does not provide.
 */
export function DashboardStatusCard({ portalBadge }: { portalBadge?: React.ReactNode } = {}) {
  const { t } = useTranslation();
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  const connected = wifiStatus?.state === "connected";

  return (
    <div className="flex flex-col gap-4 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <span className={cn("size-2.5 shrink-0 rounded-full", connected ? "bg-green-500" : "bg-muted-foreground/40")} aria-hidden />
        <span className="font-semibold">{connected ? t("dashboard.statusConnected") : t("dashboard.statusDisconnected")}</span>
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">{wifiStatus?.ssid}</span>
        {wifiStatus?.ip_address ? <Badge variant="outline">{wifiStatus.ip_address}</Badge> : null}
      </div>
      <div className="border-t border-border" />
      <dl className="flex flex-col gap-2 text-sm md:hidden">
        <KvRow label={t("dashboard.hostnameLabel")} value={systemStatus?.hostname} />
        <KvRow label={t("dashboard.deviceIdLabel")} value={systemStatus?.device_id} />
        <KvRow label={t("dashboard.portalLabel")} value={systemStatus?.versions.portal} badge={portalBadge} />
        <KvRow label={t("dashboard.sdkLabel")} value={systemStatus?.versions.sdk} />
      </dl>
      <dl className="hidden grid-cols-2 gap-4 text-sm md:grid">
        <KvRow label={t("dashboard.hostnameLabel")} value={systemStatus?.hostname} />
        <KvRow label={t("dashboard.deviceIdLabel")} value={systemStatus?.device_id} />
      </dl>
      {systemStatus && !systemStatus.ntp_synchronized ? (
        <Alert>
          <AlertDescription>{t("dashboard.ntpBanner")}</AlertDescription>
        </Alert>
      ) : null}
      {systemStatus && systemStatus.disk_free_bytes < LOW_DISK_BYTES ? (
        <Alert variant="destructive">
          <AlertDescription>
            {t("dashboard.diskLowWarning", { freeGb: (systemStatus.disk_free_bytes / 1_000_000_000).toFixed(1) })}
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}

function KvRow({
  label,
  value,
  badge,
}: {
  label: string;
  value: string | null | undefined;
  badge?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="flex items-center gap-2 font-medium">
        {value ?? "—"}
        {badge}
      </dd>
    </div>
  );
}
