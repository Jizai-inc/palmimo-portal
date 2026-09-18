import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { EnvPanel } from "@/components/EnvPanel";

/** The environment-variables screen: route + `AppShell` chrome only. Logic lives in `EnvPanel`. */
export const Route = createFileRoute("/env")({
  component: EnvScreen,
});

function EnvScreen() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("env.title")}>
      <EnvPanel />
    </AppShell>
  );
}
