import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { AppShell } from "@/components/AppShell";
import { WifiSettingsPanel } from "@/components/WifiSettingsPanel";

/**
 * Wi-Fi settings screen: route + `AppShell` chrome only. Logic lives in
 * `WifiSettingsPanel`, which takes `onReconnect` instead of calling
 * `useNavigate` itself so it's unit-testable -- see
 * components/WifiSettingsPanel.test.tsx.
 *
 * "Connect to a different network" navigates to `/wifi?reconfigure=1`,
 * flagging entry from here (see routes/wifi.tsx).
 */
export const Route = createFileRoute("/wifi-settings")({
  component: WifiSettingsScreen,
});

function WifiSettingsScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <AppShell title={t("wifiSettings.title")} subtitle={t("wifiSettings.subtitle")}>
      <WifiSettingsPanel onReconnect={() => void navigate({ to: "/wifi", search: { reconfigure: true } })} />
    </AppShell>
  );
}
