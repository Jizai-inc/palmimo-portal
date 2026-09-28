import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { GitCredentialsPanel } from "@/components/GitCredentialsPanel";

export const Route = createFileRoute("/git-credentials")({ component: GitCredentialsScreen });

function GitCredentialsScreen() {
  const { t } = useTranslation();
  return <AppShell title={t("gitCredentials.title")}><GitCredentialsPanel /></AppShell>;
}
