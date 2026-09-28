import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { AppsListPanel } from "@/components/AppsListPanel";

/** The apps list screen: route + `AppShell` chrome only. Logic lives in `AppsListPanel`. */
export const Route = createFileRoute("/apps")({
  component: AppsScreen,
});

function AppsScreen() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("apps.title")}>
      <AppsListPanel />
    </AppShell>
  );
}
