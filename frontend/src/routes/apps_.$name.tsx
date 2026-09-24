import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppDetailPanel } from "@/components/AppDetailPanel";
import { AppShell } from "@/components/AppShell";

/** The app detail screen: route + `AppShell` chrome only. Logic lives in `AppDetailPanel`. */
export const Route = createFileRoute("/apps_/$name")({
  component: AppDetailScreen,
});

function AppDetailScreen() {
  const { name } = Route.useParams();
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <AppShell title={name} breadcrumbs={[<Link key="apps" to="/apps">{t("apps.title")}</Link>, <span key="app">{name}</span>]}>
      <AppDetailPanel name={name} onDeleted={() => void navigate({ to: "/apps" })} />
    </AppShell>
  );
}
