import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1WifiStatusGetMockHandler } from "@/api/generated/wifi/wifi.msw";
import type { SystemStatus } from "@/api/generated/models";
import { DashboardStatusCard } from "@/components/DashboardStatusCard";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const BASE_SYSTEM_STATUS: SystemStatus = {
  state: "connected",
  hostname: "palmimo-405",
  auth_state: "set",
  device_id: "405",
  versions: { portal: "0.2.0", sdk: "0.5.1" },
  last_wifi_attempt: null,
  adapters: "fake",
  state_dir: "/tmp",
};

describe("DashboardStatusCard", () => {
  it("renders the ssid, ip address, and hostname from GET /wifi/status and /system/status", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "connected", ssid: "Home Wi-Fi", ip_address: "10.0.0.42" }),
      getGetStatusApiV1SystemStatusGetMockHandler(BASE_SYSTEM_STATUS),
    );

    renderWithProviders(<DashboardStatusCard />);

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getAllByText("Home Wi-Fi").length).toBeGreaterThan(0);
    expect(screen.getAllByText("10.0.0.42").length).toBeGreaterThan(0);
    expect(screen.getAllByText("palmimo-405").length).toBeGreaterThan(0);
    expect(screen.getAllByText("405").length).toBeGreaterThan(0);
  });

  it("shows a grey dot and 'Not connected' when wifi is not connected", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "disconnected", ssid: null, ip_address: null }),
      getGetStatusApiV1SystemStatusGetMockHandler({ ...BASE_SYSTEM_STATUS, state: "disconnected" }),
    );

    renderWithProviders(<DashboardStatusCard />);

    expect(await screen.findByText("Not connected")).toBeInTheDocument();
  });

  it("renders no portal badge by default", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "connected", ssid: "Home Wi-Fi", ip_address: "10.0.0.42" }),
      getGetStatusApiV1SystemStatusGetMockHandler(BASE_SYSTEM_STATUS),
    );

    renderWithProviders(<DashboardStatusCard />);

    await screen.findByText("Connected");
    expect(screen.queryByText("update badge")).not.toBeInTheDocument();
  });

  it("renders the given portalBadge slot next to the Portal version, router-free", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "connected", ssid: "Home Wi-Fi", ip_address: "10.0.0.42" }),
      getGetStatusApiV1SystemStatusGetMockHandler(BASE_SYSTEM_STATUS),
    );

    renderWithProviders(<DashboardStatusCard portalBadge={<span>update badge</span>} />);

    expect(await screen.findByText("update badge")).toBeInTheDocument();
  });
});
