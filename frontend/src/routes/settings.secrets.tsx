import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { EnvPanel } from "@/components/EnvPanel";
import { SettingsSection } from "@/components/SettingsSection";

export const Route = createFileRoute("/settings/secrets")({
  component: SecretsSettings,
});

function SecretsSettings() {
  const { t } = useTranslation();
  return (
    <SettingsSection title={t("nav.secrets")}>
      <EnvPanel />
    </SettingsSection>
  );
}
