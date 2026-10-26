import type { LucideIcon } from "lucide-react";
import { Cpu, Monitor, Play, Wifi } from "lucide-react";
import type * as React from "react";
import { useTranslation } from "react-i18next";

import { useGetPlatformApiV1PlatformGet } from "@/api/generated/platform/platform";
import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useGetStatusApiV1WifiStatusGet } from "@/api/generated/wifi/wifi";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { WifiQrSetupNotice } from "@/components/WifiQrSetupNotice";
import { cn } from "@/lib/utils";

/** Below this, `dashboard.diskLowWarning` is shown (design doc 3.4's own 1 GB free-space gate). */
const LOW_DISK_BYTES = 1_000_000_000;

/**
 * Home's four status tiles plus the clock and disk warnings. Fetches its own data so it can be
 * rendered standalone; `portalBadge` and `appsLink` are slots because `<Link>` needs a router
 * context the standalone test harness does not provide.
 */
export function DashboardStatusCard({ portalBadge, appsLink }: { portalBadge?: React.ReactNode; appsLink?: React.ReactNode } = {}) {
  const { t } = useTranslation();
  const { data: wifiStatus } = useGetStatusApiV1WifiStatusGet();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet({ query: { refetchInterval: 2_000 } });
  const { data: platform } = useGetPlatformApiV1PlatformGet();
  const connected = wifiStatus?.state === "connected";
  const runningApp = systemStatus?.running_app;
  const sdk = systemStatus?.versions.sdk;
  const platformBlocked = platform?.ready === false;
  const platformVersion = platform?.installed_version != null ? t("home.tilePlatformVersion", { version: platform.installed_version }) : "—";

  return (
    <div className="flex flex-col gap-3">
      <WifiQrSetupNotice setup={systemStatus?.wifi_setup} />
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
      <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatusTile icon={Wifi} label={t("home.tileNetwork")}>
          <span className="flex min-w-0 items-center gap-2">
            <span className={cn("size-2 shrink-0 rounded-full", connected ? "bg-success" : "bg-muted-foreground/40")} aria-hidden />
            <span className="truncate">{connected ? wifiStatus?.ssid : t("dashboard.statusDisconnected")}</span>
          </span>
          <TileNote mono>{wifiStatus?.ip_address ?? "—"}</TileNote>
        </StatusTile>
        <StatusTile icon={Play} label={t("home.tileRunningApp")}>
          <span className={cn("truncate", !runningApp && "text-muted-foreground")}>{runningApp?.name ?? t("home.tileNone")}</span>
          {appsLink ? <TileNote>{appsLink}</TileNote> : null}
        </StatusTile>
        <StatusTile icon={Monitor} label={t("dashboard.portalLabel")}>
          <span className="flex items-center gap-2">
            {systemStatus?.versions.portal ?? "—"}
            {portalBadge}
          </span>
          <TileNote>{sdk ? t("home.tileSdk", { version: sdk }) : t("home.tileSdkMissing")}</TileNote>
        </StatusTile>
        <StatusTile icon={Cpu} label={t("update.platformTitle")} warning={platformBlocked}>
          <span>
            {platformVersion} · {platformBlocked ? t("home.tilePlatformUpdateRequired") : t("update.platformUpToDate")}
          </span>
          {platformBlocked ? <TileNote>{t("home.tilePlatformBlocked")}</TileNote> : null}
        </StatusTile>
      </dl>
    </div>
  );
}

function StatusTile({
  icon: Icon,
  label,
  warning = false,
  children,
}: {
  icon: LucideIcon;
  label: string;
  warning?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-2 rounded-xl border p-4",
        warning ? "border-warning-border bg-warning-soft [&_.tile-muted]:text-warning-foreground" : "border-border bg-card",
      )}
    >
      <dt className="tile-muted flex items-center gap-1.5 text-xs text-muted-foreground">
        <Icon className="size-3.5" aria-hidden />
        {label}
      </dt>
      <dd className="flex min-w-0 flex-col gap-2 text-base font-semibold">{children}</dd>
    </div>
  );
}

function TileNote({ mono = false, children }: { mono?: boolean; children: React.ReactNode }) {
  return <span className={cn("tile-muted truncate text-xs font-normal text-muted-foreground", mono && "font-mono")}>{children}</span>;
}

/** Home's right-hand device facts. */
export function DeviceInfoCard() {
  const { t } = useTranslation();
  const { data: systemStatus } = useGetStatusApiV1SystemStatusGet();
  return (
    <section aria-labelledby="home-device" className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 text-[13px]">
      <h2 id="home-device" className="text-[15px] font-semibold">
        {t("nav.device")}
      </h2>
      <dl className="flex flex-col gap-3">
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">{t("dashboard.hostnameLabel")}</dt>
          <dd className="truncate">{systemStatus?.hostname ?? "—"}</dd>
        </div>
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">{t("dashboard.deviceIdLabel")}</dt>
          <dd className={cn("truncate", !systemStatus?.device_id && "text-muted-foreground")}>
            {systemStatus ? (systemStatus.device_id ?? t("home.deviceIdUnassigned")) : "—"}
          </dd>
        </div>
      </dl>
    </section>
  );
}
