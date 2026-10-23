import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { GitCredentialsPanel } from "@/components/GitCredentialsPanel";
import { SettingsSection } from "@/components/SettingsSection";
import { SshKeysPanel } from "@/components/SshKeysPanel";

export const Route = createFileRoute("/settings/developer")({
  component: DeveloperSettings,
});

function DeveloperSettings() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-8">
      <SettingsSection title={t("sshKeys.title")}>
        <SshKeysPanel />
      </SettingsSection>
      <SettingsSection title={t("gitCredentials.title")}>
        <GitCredentialsPanel />
      </SettingsSection>
    </div>
  );
}
