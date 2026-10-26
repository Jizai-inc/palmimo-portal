import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { WifiSettingsPanel } from "@/components/WifiSettingsPanel";

/** "Connect to a different network" reuses the setup scan screen, flagged by `?reconfigure=1` (see routes/wifi.tsx). */
export const Route = createFileRoute("/settings/network")({
  component: NetworkSettings,
});

function NetworkSettings() {
  const navigate = useNavigate();
  return (
    <WifiSettingsPanel
      onReconnect={() => void navigate({ to: "/wifi", search: { reconfigure: true } })}
      onAdd={() => void navigate({ to: "/wifi", search: { add: true } })}
    />
  );
}
