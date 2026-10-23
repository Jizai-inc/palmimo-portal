import { Outlet, createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";

export const Route = createFileRoute("/settings")({
  component: SettingsLayout,
});

function SettingsLayout() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("nav.settings")} alignStart>
      <Outlet />
    </AppShell>
  );
}
