import { Link, createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppLogsPanel } from "@/components/AppLogsPanel";
import { AppShell } from "@/components/AppShell";

/**
 * The full-page app logs screen: route + `AppShell` chrome only. Logic lives in `AppLogsPanel`.
 * Filename uses TanStack Router's trailing-underscore escape twice (`apps_.$id_.logs.tsx`, not
 * `apps.$id.logs.tsx`) so this is a sibling of `/apps/$id`, not nested under it -- `/apps/$id`
 * (routes/apps_.$id.tsx) renders no `<Outlet/>` (see wifi_.waiting.tsx for the same escape).
 */
export const Route = createFileRoute("/apps_/$id_/logs")({
  component: AppLogsScreen,
});

function AppLogsScreen() {
  const { id } = Route.useParams();
  const { t } = useTranslation();
  return (
    <AppShell title={id} breadcrumbs={[<Link key="apps" to="/apps">{t("apps.title")}</Link>, <Link key="app" to="/apps/$id" params={{ id }}>{id}</Link>, <span key="logs">{t("appDetail.logsTitle")}</span>]}>
      <AppLogsPanel id={id} />
    </AppShell>
  );
}
