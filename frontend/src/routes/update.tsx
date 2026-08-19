import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { UpdatePanel } from "@/components/UpdatePanel";

/**
 * Portal/SDK update screen: route + `AppShell` chrome only. Logic lives in
 * `UpdatePanel`, which takes `onRestarted` instead of reloading the page
 * itself so it's unit-testable -- see components/UpdatePanel.test.tsx.
 */
export const Route = createFileRoute("/update")({
  component: UpdateScreen,
});

function UpdateScreen() {
  const { t } = useTranslation();
  return (
    <AppShell title={t("update.title")}>
      <UpdatePanel />
    </AppShell>
  );
}
