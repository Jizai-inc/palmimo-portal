import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { GitCredentialsPanel } from "@/components/GitCredentialsPanel";
import { PageHeader, SectionHeader } from "@/components/PageHeader";
import { SshKeysPanel } from "@/components/SshKeysPanel";

export const Route = createFileRoute("/settings/developer")({
  component: DeveloperSettings,
});

function DeveloperSettings() {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-8">
      <PageHeader title={t("nav.developer")} description={t("developer.pageDescription")} />
      <section className="flex flex-col gap-3.5">
        <SectionHeader title={t("sshKeys.title")} description={t("sshKeys.body")} />
        <SshKeysPanel />
      </section>
      <GitCredentialsPanel />
    </div>
  );
}
