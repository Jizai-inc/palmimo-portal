import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { HomePanel } from "@/components/HomePanel";

export const Route = createFileRoute("/home")({
  component: HomeScreen,
});

function HomeScreen() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("dashboard.title")}>
      <HomePanel />
    </AppShell>
  );
}
