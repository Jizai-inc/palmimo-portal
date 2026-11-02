import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { useGetAppApiV1AppsNameGet } from "@/api/generated/apps/apps";
import { AppDetailPanel } from "@/components/AppDetailPanel";
import { AppShell } from "@/components/AppShell";

/** The app detail screen: route + `AppShell` chrome only. Logic lives in `AppDetailPanel`. */
export const Route = createFileRoute("/apps_/$id")({
  component: AppDetailScreen,
});

function AppDetailScreen() {
  const { id } = Route.useParams();
  const { t } = useTranslation();
  const navigate = useNavigate();
  // Reads the detail panel's cached query without fetching on its own.
  const { data: app } = useGetAppApiV1AppsNameGet(id, { query: { enabled: false } });
  const title = app?.label ?? id;
  return (
    <AppShell title={title} breadcrumbs={[<Link key="apps" to="/apps">{t("apps.title")}</Link>, <span key="app">{title}</span>]}>
      <AppDetailPanel id={id} onDeleted={() => void navigate({ to: "/apps" })} />
    </AppShell>
  );
}
