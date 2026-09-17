import { createFileRoute } from "@tanstack/react-router";

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
  return (
    <AppShell title={name}>
      <AppLogsPanel name={name} />
    </AppShell>
  );
}
