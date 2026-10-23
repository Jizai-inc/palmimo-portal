import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { SettingsSection } from "@/components/SettingsSection";
import { UpdatePanel } from "@/components/UpdatePanel";

export const Route = createFileRoute("/settings/update")({
  component: UpdateSettings,
});

function UpdateSettings() {
  const { t } = useTranslation();
  return (
    <SettingsSection title={t("nav.softwareUpdate")}>
      <UpdatePanel />
    </SettingsSection>
  );
}
