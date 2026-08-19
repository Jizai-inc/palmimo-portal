import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { WifiSettingsPanel } from "@/components/WifiSettingsPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function jsonError(status: number, code: string, params: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, params } }, { status });
}

function stubStatuses({
  wifi,
  lastAttempt,
}: {
  wifi?: Partial<{ state: string; ssid: string | null; ip_address: string | null }>;
  lastAttempt?: { ssid: string; result: string; timestamp: number } | null;
} = {}) {
  server.use(
    http.get("*/api/v1/wifi/status", () =>
      HttpResponse.json({ state: "connected", ssid: "HomeNet", ip_address: "198.51.100.7", ...wifi }),
    ),
    http.get("*/api/v1/system/status", () =>
      HttpResponse.json({
        state: "connected",
        hostname: "palmimo-1234",
        auth_state: "set",
        device_id: "1234",
        versions: { portal: "0.1.0", sdk: null },
        last_wifi_attempt: lastAttempt === undefined ? null : lastAttempt,
        adapters: "fake",
        state_dir: "/tmp",
      }),
    ),
  );
}

describe("WifiSettingsPanel", () => {
  it("renders the current ssid, IP address, hostname, and last attempt", async () => {
    stubStatuses({ lastAttempt: { ssid: "HomeNet", result: "connected", timestamp: 0 } });
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    expect(await screen.findByText("HomeNet")).toBeInTheDocument();
    expect(screen.getByText("198.51.100.7")).toBeInTheDocument();
    expect(screen.getByText("palmimo-1234")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText(/^Connected \(/)).toBeInTheDocument();
  });

  it('renders "Not connected" and dashes when there is no connection or attempt on record', async () => {
    stubStatuses({ wifi: { state: "unprovisioned", ssid: null, ip_address: null } });
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    expect(await screen.findByText("Not connected")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("forget: dialog -> confirm -> DELETE called -> terminal state shown", async () => {
    stubStatuses();
    const user = userEvent.setup();
    let forgetCalled = false;
    server.use(
      http.delete("*/api/v1/wifi/connection", () => {
        forgetCalled = true;
        return HttpResponse.json({ status: "forgetting" });
      }),
    );
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Forget this network" }));
    expect(await screen.findByText("Forget this network?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Forget" }));

    await waitFor(() => expect(forgetCalled).toBe(true));
    expect(await screen.findByText("Palmimo is switching back to its own Wi-Fi")).toBeInTheDocument();
    expect(screen.getByText(/palmimo-1234/)).toBeInTheDocument();
  });

  it("treats a network-level DELETE failure as success -- the device dropping the LAN mid-response is expected", async () => {
    stubStatuses();
    const user = userEvent.setup();
    server.use(http.delete("*/api/v1/wifi/connection", () => HttpResponse.error()));
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Forget this network" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Forget" }));

    expect(await screen.findByText("Palmimo is switching back to its own Wi-Fi")).toBeInTheDocument();
    expect(
      screen.getByText("The connection dropped before Palmimo answered — that is expected."),
    ).toBeInTheDocument();
  });

  it("shows an error alert on a 503 and does not switch to the terminal state", async () => {
    stubStatuses();
    const user = userEvent.setup();
    server.use(http.delete("*/api/v1/wifi/connection", () => jsonError(503, "network_backend_unavailable")));
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Forget this network" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Forget" }));

    expect(
      await screen.findByText("Palmimo could not reach its Wi-Fi service. Try again shortly."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Palmimo is switching back to its own Wi-Fi")).not.toBeInTheDocument();
  });

  it("reconnect: dialog -> confirm calls onReconnect, without hitting the forget endpoint", async () => {
    stubStatuses();
    const user = userEvent.setup();
    const onReconnect = vi.fn();
    let forgetCalled = false;
    server.use(
      http.delete("*/api/v1/wifi/connection", () => {
        forgetCalled = true;
        return HttpResponse.json({ status: "forgetting" });
      }),
    );
    renderWithProviders(<WifiSettingsPanel onReconnect={onReconnect} />);

    await user.click(await screen.findByRole("button", { name: "Connect to a different network" }));
    expect(await screen.findByText("Connect to a different network?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Continue" }));

    expect(onReconnect).toHaveBeenCalledTimes(1);
    expect(forgetCalled).toBe(false);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("disables the forget button and shows a loading label while the hostname is unknown", async () => {
    server.use(
      http.get("*/api/v1/wifi/status", () =>
        HttpResponse.json({ state: "connected", ssid: "HomeNet", ip_address: "198.51.100.7" }),
      ),
      // Never resolves -- system/status (and so `hostname`) stays unknown.
      http.get("*/api/v1/system/status", () => new Promise(() => {})),
    );
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    const button = await screen.findByRole("button", { name: "Loading…" });
    expect(button).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Forget this network" })).not.toBeInTheDocument();
  });

  it("issues no request when the forget dialog is cancelled", async () => {
    stubStatuses();
    const user = userEvent.setup();
    let forgetCalled = false;
    server.use(
      http.delete("*/api/v1/wifi/connection", () => {
        forgetCalled = true;
        return HttpResponse.json({ status: "forgetting" });
      }),
    );
    renderWithProviders(<WifiSettingsPanel onReconnect={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Forget this network" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(forgetCalled).toBe(false);
  });
});
