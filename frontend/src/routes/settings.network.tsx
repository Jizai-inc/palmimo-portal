import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

import { SettingsSection } from "@/components/SettingsSection";
import { WifiSettingsPanel } from "@/components/WifiSettingsPanel";

/** "Connect to a different network" reuses the setup scan screen, flagged by `?reconfigure=1` (see routes/wifi.tsx). */
export const Route = createFileRoute("/settings/network")({
  component: NetworkSettings,
});

function NetworkSettings() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <SettingsSection title={t("nav.network")} subtitle={t("wifiSettings.subtitle")}>
      <WifiSettingsPanel
        onReconnect={() => void navigate({ to: "/wifi", search: { reconfigure: true } })}
        onAdd={() => void navigate({ to: "/wifi", search: { add: true } })}
      />
    </SettingsSection>
  );
}
