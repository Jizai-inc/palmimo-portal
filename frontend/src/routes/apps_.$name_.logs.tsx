import { Link, createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppLogsPanel } from "@/components/AppLogsPanel";
import { AppShell } from "@/components/AppShell";

/**
 * The full-page app logs screen: route + `AppShell` chrome only. Logic lives in `AppLogsPanel`.
 * Filename uses TanStack Router's trailing-underscore escape twice (`apps_.$name_.logs.tsx`, not
 * `apps.$name.logs.tsx`) so this is a sibling of `/apps/$name`, not nested under it -- `/apps/$name`
 * (routes/apps_.$name.tsx) renders no `<Outlet/>` (see wifi_.waiting.tsx for the same escape).
 */
export const Route = createFileRoute("/apps_/$name_/logs")({
  component: AppLogsScreen,
});

function AppLogsScreen() {
  const { name } = Route.useParams();
  const { t } = useTranslation();
  return (
    <AppShell title={name} breadcrumbs={[<Link key="apps" to="/apps">{t("apps.title")}</Link>, <Link key="app" to="/apps/$name" params={{ name }}>{name}</Link>, <span key="logs">{t("appDetail.logsTitle")}</span>]}>
      <AppLogsPanel name={name} />
    </AppShell>
  );
}
