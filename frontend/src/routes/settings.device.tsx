import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { PowerPanel } from "@/components/PowerPanel";
import { SettingsSection } from "@/components/SettingsSection";

interface DeviceSearch {
  /** Set by the header's device menu to open that confirm dialog on arrival. */
  action?: "restart" | "shutdown";
}

export const Route = createFileRoute("/settings/device")({
  validateSearch: (search: Record<string, unknown>): DeviceSearch =>
    search.action === "restart" || search.action === "shutdown" ? { action: search.action } : {},
  component: DeviceSettings,
});

function DeviceSettings() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { action } = Route.useSearch();
  // Each header request remounts the panel with that dialog open, also when already on this
  // page; clearing `?action=` afterwards keeps a reload from reopening it.
  const [request, setRequest] = useState<{ action: NonNullable<DeviceSearch["action"]>; id: number }>();

  useEffect(() => {
    if (!action) return;
    setRequest((previous) => ({ action, id: (previous?.id ?? 0) + 1 }));
    void navigate({ to: "/settings/device", search: {}, replace: true });
  }, [action, navigate]);

  return (
    <SettingsSection title={t("nav.device")}>
      {/*
        "/" re-runs the root guard (routes/__root.tsx), which lands back on
        `/home` once the device is reachable again -- sessions survive a
        reboot because the cookie signing key lives in auth.json.
      */}
      <PowerPanel key={request?.id} initialDialog={request?.action}onRebooted={() => void navigate({ to: "/" })} />
    </SettingsSection>
  );
}
