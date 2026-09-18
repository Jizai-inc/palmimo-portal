import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AddAppPanel } from "@/components/AddAppPanel";
import { AppShell } from "@/components/AppShell";

/** The add-app screen: route + `AppShell` chrome only. Logic lives in `AddAppPanel`. */
export const Route = createFileRoute("/apps_/add")({
  component: AddAppScreen,
});

function AddAppScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <AppShell title={t("appAdd.title")}>
      <AddAppPanel
        onInstalled={(appName) =>
          void (appName ? navigate({ to: "/apps/$name", params: { name: appName } }) : navigate({ to: "/apps" }))
        }
      />
    </AppShell>
  );
}
