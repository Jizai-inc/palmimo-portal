import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { PowerPanel } from "@/components/PowerPanel";

/**
 * Power-controls screen: route + `AppShell` chrome only. Logic lives in
 * `PowerPanel`, which takes `onRebooted` instead of calling `useNavigate`
 * itself so it's unit-testable -- see components/PowerPanel.test.tsx.
 */
export const Route = createFileRoute("/power")({
  component: PowerScreen,
});

function PowerScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <AppShell title={t("power.title")}>
      {/*
        "/" re-runs the root guard's `beforeLoad` (see routes/__root.tsx),
        which re-resolves `system/status` from scratch and lands back on
        `/dashboard` once the device is reachable again -- sessions survive
        a reboot because the cookie signing key lives in auth.json (see
        this component's own docstring and PowerPanel.test.tsx).
      */}
      <PowerPanel onRebooted={() => void navigate({ to: "/" })} />
    </AppShell>
  );
}
