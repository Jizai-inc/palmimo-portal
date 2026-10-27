import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useListAppsApiV1AppsGet } from "@/api/generated/apps/apps";

import { AddAppDialog } from "@/components/AddAppDialog";
import { AppShell } from "@/components/AppShell";
import { AppsListPanel } from "@/components/AppsListPanel";
import { DiscoverPanel } from "@/components/DiscoverPanel";
import { Button } from "@/components/ui/button";
import { tabClassName } from "@/lib/tabClassName";

interface AppsSearch {
  tab?: "discover";
  /** Opens the add-app dialog on that source; `/apps/add` redirects here with it set. */
  add?: "github" | "zip";
}

/** The Apps screen: Installed and Discover tabs, selected by `?tab=` so the checklist can link straight to Discover. */
export const Route = createFileRoute("/apps")({
  validateSearch: (search: Record<string, unknown>): AppsSearch => ({
    ...(search.tab === "discover" ? { tab: "discover" } : {}),
    ...(search.add === "github" || search.add === "zip" ? { add: search.add } : {}),
  }),
  component: AppsScreen,
});

function AppsScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { tab, add } = Route.useSearch();
  const discover = tab === "discover";
  const { data: appsData } = useListAppsApiV1AppsGet();
  const goToInstalled = (appId: string | null) =>
    void (appId ? navigate({ to: "/apps/$id", params: { id: appId } }) : navigate({ to: "/apps", search: {} }));

  return (
    <AppShell
      title={t("apps.title")}
      description={t("nav.appsDescription")}
      actions={
        <Button asChild>
          <Link to="/apps" search={{ ...(discover ? { tab: "discover" as const } : {}), add: "github" }}>
            <Plus className="size-4" aria-hidden />
            {t("apps.addFromUrlButton")}
          </Link>
        </Button>
      }
    >
      <nav aria-label={t("apps.tabsLabel")} className="-mb-1 flex gap-6 border-b border-border">
        <Link to="/apps" search={{}} aria-current={discover ? undefined : "page"} className={tabClassName(!discover)}>
          {t("apps.tabInstalled")}
          {appsData ? <span className="ml-1.5 font-normal text-muted-foreground">{appsData.apps.length}</span> : null}
        </Link>
        <Link to="/apps" search={{ tab: "discover" }} aria-current={discover ? "page" : undefined} className={tabClassName(discover)}>
          {t("apps.tabDiscover")}
        </Link>
      </nav>
      {discover ? <DiscoverPanel onInstalled={goToInstalled} /> : <AppsListPanel />}
      <AddAppDialog
        open={add !== undefined}
        initialTab={add}
        onOpenChange={(open) => {
          if (!open) void navigate({ to: "/apps", search: discover ? { tab: "discover" } : {}, replace: true });
        }}
        onInstalled={goToInstalled}
      />
    </AppShell>
  );
}
