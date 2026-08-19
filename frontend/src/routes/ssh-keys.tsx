import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { SshKeysPanel } from "@/components/SshKeysPanel";

/**
 * The SSH-keys screen: route definition + `AppShell` chrome only. All the
 * actual logic lives in `SshKeysPanel` (no router hooks there), so it can
 * be unit-tested directly -- see components/SshKeysPanel.test.tsx.
 */
export const Route = createFileRoute("/ssh-keys")({
  component: SshKeysScreen,
});

function SshKeysScreen() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("sshKeys.title")}>
      <SshKeysPanel />
    </AppShell>
  );
}
