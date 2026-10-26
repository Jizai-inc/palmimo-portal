import { Link } from "@tanstack/react-router";
import type { TFunction } from "i18next";
import { Package, Wifi } from "lucide-react";
import { useTranslation } from "react-i18next";

import { formatLocalTimestamp } from "@/lib/formatTimestamp";
import type { ActivityItem } from "@/lib/recentActivity";

function activityText(t: TFunction, item: ActivityItem): string {
  if (item.kind === "wifi") {
    return item.ok ? t("home.activityWifiConnected", { ssid: item.ssid }) : t("home.activityWifiFailed", { ssid: item.ssid });
  }
  const name = item.appName;
  switch (item.job) {
    case "install":
      return item.ok ? t("home.activityInstalled", { name }) : t("home.activityInstallFailed", { name });
    case "update":
      return item.ok ? t("home.activityUpdated", { name }) : t("home.activityUpdateFailed", { name });
    case "delete":
      return item.ok ? t("home.activityDeleted", { name }) : t("home.activityDeleteFailed", { name });
  }
}

export function RecentActivityCard({ items }: { items: readonly ActivityItem[] }) {
  const { t, i18n } = useTranslation();
  return (
    <section aria-labelledby="home-activity" className="flex flex-col gap-3.5 rounded-xl border border-border bg-card p-5">
      <h2 id="home-activity" className="text-[15px] font-semibold">
        {t("home.activityTitle")}
      </h2>
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("home.activityEmpty")}</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => {
            const Icon = item.kind === "wifi" ? Wifi : Package;
            const text = activityText(t, item);
            return (
              <li key={item.kind === "wifi" ? "wifi" : `app:${item.appId}`} className="flex items-center gap-3 text-sm">
                <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                <span className="min-w-0 flex-1">
                  {item.kind === "appJob" && item.job !== "delete" ? (
                    <Link to="/apps/$id" params={{ id: item.appId }} className="hover:underline">
                      {text}
                    </Link>
                  ) : (
                    text
                  )}
                </span>
                <time className="shrink-0 text-xs text-muted-foreground" dateTime={new Date(item.at * 1000).toISOString()}>
                  {formatLocalTimestamp(item.at, { withYear: false, locale: i18n.language })}
                </time>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
