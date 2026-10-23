import { ChevronRight } from "lucide-react";
import { Link, createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { useGetStatusApiV1UpdateStatusGet } from "@/api/generated/update/update";
import { AppShell } from "@/components/AppShell";
import { DashboardStatusCard } from "@/components/DashboardStatusCard";
import { Badge } from "@/components/ui/badge";
import { NAV_ITEMS, type NavItem, SETTINGS_ITEMS } from "@/lib/navigation";
import { navDescription, navLabel } from "@/lib/navLabels";
import { cn } from "@/lib/utils";

const QUICK_ACTIONS: readonly NavItem[] = [...NAV_ITEMS.filter((item) => item.to === "/apps"), ...SETTINGS_ITEMS];

/** Home: a status card, the software-version card (desktop), and quick actions into Apps and each Settings section. */
export const Route = createFileRoute("/dashboard")({
  component: DashboardScreen,
});

function DashboardScreen() {
  const { t } = useTranslation();
  // Polls so the status card reflects reality without a manual refresh
  // (see palmimo-portal-technical.md's polling note).
  const { data: status } = useGetStatusApiV1SystemStatusGet({ query: { refetchInterval: 10_000 } });
  // Cached read only -- never fires POST /update/check; UpdatePanel.tsx owns
  // the auto-check-once-per-visit logic.
  const { data: updateStatus } = useGetStatusApiV1UpdateStatusGet();
  const updateBadge = updateStatus?.update_available ? (
    <Link to="/settings/update">
      <Badge>{t("dashboard.updateAvailableBadge")}</Badge>
    </Link>
  ) : undefined;

  return (
    <AppShell title={t("dashboard.title")}>
      <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
        <div className="flex min-w-0 flex-1 flex-col gap-6">
          <DashboardStatusCard portalBadge={updateBadge} />

          {/* Version card -- desktop only (mobile shows Portal/SDK inside DashboardStatusCard's own KV list). */}
          <div className="hidden flex-col gap-3 rounded-xl border border-border bg-card p-4 md:flex">
            <p className="font-semibold">{t("dashboard.softwareTitle")}</p>
            <dl className="flex flex-col gap-2 text-sm">
              <div className="flex items-center justify-between">
                <dt className="text-muted-foreground">{t("dashboard.portalLabel")}</dt>
                <dd className="flex items-center gap-2 font-medium">
                  {status?.versions.portal ?? "—"}
                  {updateBadge}
                </dd>
              </div>
              <div className="flex items-center justify-between">
                <dt className="text-muted-foreground">{t("dashboard.sdkLabel")}</dt>
                <dd className="font-medium">{status?.versions.sdk ?? "—"}</dd>
              </div>
            </dl>
          </div>

          {/* Nav list -- mobile only (desktop shows the same items in the quick-actions card). */}
          <NavListCard items={QUICK_ACTIONS} className="md:hidden" />
        </div>

        <div className="w-full lg:w-[360px] lg:shrink-0">
          <NavListCard items={QUICK_ACTIONS} title={t("dashboard.quickActionsTitle")} className="hidden md:flex" />
        </div>
      </div>
    </AppShell>
  );
}

function NavListCard({
  items,
  title,
  className,
}: {
  items: readonly NavItem[];
  title?: string;
  className?: string;
}) {
  const { t } = useTranslation();
  return (
    <div className={cn("flex flex-col gap-1 rounded-xl border border-border bg-card p-2", className)}>
      {title ? <p className="px-2 py-1.5 text-sm font-semibold">{title}</p> : null}
      {items.map((item) => {
        const Icon = item.icon;
        return (
          <Link key={item.to} to={item.to} className="flex items-center gap-3 rounded-lg p-2 hover:bg-secondary">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted">
              <Icon className="size-4" />
            </span>
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="text-sm font-medium">{navLabel(t, item.labelKey)}</span>
              {navDescription(t, item.descriptionKey) ? (
                <span className="truncate text-xs text-muted-foreground">{navDescription(t, item.descriptionKey)}</span>
              ) : null}
            </span>
            <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
          </Link>
        );
      })}
    </div>
  );
}
