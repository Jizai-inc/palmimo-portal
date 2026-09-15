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
  disk_free_bytes: 5_000_000_000,
  ntp_synchronized: true,
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

  // Without these, a device stuck before NTP sync (GitHub/PyPI TLS failing, catalog looking
  // empty -- design doc 3.6) or one about to hit ENOSPC mid-install would give the operator no
  // warning until an install/update job failed with a much less obvious error.
  it("shows a time-sync banner when the clock is not NTP-synchronized", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "connected", ssid: "Home Wi-Fi", ip_address: "10.0.0.42" }),
      getGetStatusApiV1SystemStatusGetMockHandler({ ...BASE_SYSTEM_STATUS, ntp_synchronized: false }),
    );

    renderWithProviders(<DashboardStatusCard />);

    expect(await screen.findByText("Waiting for time sync…")).toBeInTheDocument();
  });

  it("shows a low-disk warning under 1 GB free", async () => {
    server.use(
      getGetStatusApiV1WifiStatusGetMockHandler({ state: "connected", ssid: "Home Wi-Fi", ip_address: "10.0.0.42" }),
      getGetStatusApiV1SystemStatusGetMockHandler({ ...BASE_SYSTEM_STATUS, disk_free_bytes: 500_000_000 }),
    );

    renderWithProviders(<DashboardStatusCard />);

    expect(await screen.findByText("This device is low on disk space (0.5 GB free).")).toBeInTheDocument();
  });
});
