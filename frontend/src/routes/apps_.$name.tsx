import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { AppDetailPanel } from "@/components/AppDetailPanel";
import { AppShell } from "@/components/AppShell";

/** The app detail screen: route + `AppShell` chrome only. Logic lives in `AppDetailPanel`. */
export const Route = createFileRoute("/apps_/$name")({
  component: AppDetailScreen,
});

function AppDetailScreen() {
  const { name } = Route.useParams();
  const navigate = useNavigate();
  return (
    <AppShell title={name}>
      <AppDetailPanel name={name} onDeleted={() => void navigate({ to: "/apps" })} />
    </AppShell>
  );
}
