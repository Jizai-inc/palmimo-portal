import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { PortalApiError } from "@/api/client";
import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { WifiWaitingPanel } from "@/components/WifiWaitingPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const CONNECTING_STATUS = {
  state: "connecting",
  hostname: "palmimo-1234",
  auth_state: "set",
  device_id: "1234",
  versions: { portal: "0.1.0", sdk: null },
  last_wifi_attempt: null,
  adapters: "fake",
  state_dir: "/tmp",
};

describe("WifiWaitingPanel", () => {
  it("shows the probing status, then calls assign with the device's .local URL once the probe resolves", async () => {
    let probeCallCount = 0;
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(CONNECTING_STATUS),
      http.get("http://palmimo-1234.local/", () => {
        probeCallCount += 1;
        // The fail-first rule (see src/lib/portalProbe.ts) only trusts a
        // success once an earlier probe has failed -- the first response
        // here must fail so the second one is the one that counts.
        return probeCallCount === 1 ? HttpResponse.error() : HttpResponse.text("");
      }),
    );
    const assign = vi.fn();

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={vi.fn()}
        pollIntervalMs={50_000}
        probeIntervalMs={20}
        assign={assign}
      />,
    );

    expect(await screen.findByText("Looking for Palmimo…")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "http://palmimo-1234.local" })).toBeInTheDocument();

    await waitFor(() => expect(assign).toHaveBeenCalledWith("http://palmimo-1234.local/"));
    expect(await screen.findByText("Found — switching…")).toBeInTheDocument();
  });

  it("renders a Back-to-scan link in the base waiting view, and it calls onFailed", async () => {
    // Every other outcome view offers a Back/Try-again action; this base
    // "still waiting" view needs one too so a visitor isn't stuck until the
    // 5-minute timeout. Uses a distinct label (wifi.waitingBackToScan)
    // since it navigates away while the connect attempt is still in
    // flight -- it must not read as cancelling it.
    server.use(getGetStatusApiV1SystemStatusGetMockHandler(CONNECTING_STATUS));
    const onFailed = vi.fn();
    const user = userEvent.setup();

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        pollIntervalMs={50_000}
        probeIntervalMs={50_000}
      />,
    );

    const backButton = await screen.findByRole("button", {
      name: "Back to the network list (the connection attempt continues)",
    });
    await user.click(backButton);

    expect(onFailed).toHaveBeenCalled();
  });

  it("falls back to the generic hostname phrase in the body before system/status has resolved", async () => {
    server.use(http.get("*/api/v1/system/status", async () => new Promise(() => {})));

    renderWithProviders(
      <WifiWaitingPanel ssid="Home Wi-Fi" onConnected={vi.fn()} onFailed={vi.fn()} pollIntervalMs={50_000} probeIntervalMs={50_000} />,
    );

    expect(
      await screen.findByText(
        "Palmimo is switching to your Wi-Fi network “Home Wi-Fi”. The Wi-Fi that Palmimo itself broadcasts (“this device”) will disappear in a few seconds — that is expected.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the success screen and wires onConnected once the same-origin poll observes state: connected", async () => {
    const onConnected = vi.fn();
    server.use(
      http.get("*/api/v1/system/status", () => HttpResponse.json({ ...CONNECTING_STATUS, state: "connected" })),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={onConnected}
        onFailed={vi.fn()}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    const button = await screen.findByRole("button", { name: "Go to dashboard" });
    expect(screen.getByText('Palmimo connected to "Home Wi-Fi".')).toBeInTheDocument();

    button.click();
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it("keeps waiting when connected is observed but the attempt still names the old network, then succeeds once it resolves", async () => {
    const onConnected = vi.fn();
    let pollCount = 0;
    server.use(
      http.get("*/api/v1/system/status", () => {
        pollCount += 1;
        if (pollCount === 1) {
          // The reconfigure race: comitup still observably CONNECTED to the
          // *old* network for a moment right after the request -- must not
          // be read as success yet.
          return HttpResponse.json({
            ...CONNECTING_STATUS,
            state: "connected",
            last_wifi_attempt: { ssid: "Home Wi-Fi", result: "attempting", timestamp: 0 },
          });
        }
        return HttpResponse.json({
          ...CONNECTING_STATUS,
          state: "connected",
          last_wifi_attempt: { ssid: "Home Wi-Fi", result: "connected", timestamp: 1 },
        });
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={onConnected}
        onFailed={vi.fn()}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    const button = await screen.findByRole("button", { name: "Go to dashboard" });
    button.click();
    expect(onConnected).toHaveBeenCalledTimes(1);
    // If the first poll had wrongly resolved "connected" already (ignoring
    // that the attempt still named the old network), the effect's
    // outcome-is-still-"waiting" guard would have stopped polling right
    // there and this would never reach 2.
    expect(pollCount).toBeGreaterThanOrEqual(2);
  });

  it("shows the failure screen and wires onFailed once the same-origin poll observes a failed attempt", async () => {
    const onFailed = vi.fn();
    server.use(
      http.get("*/api/v1/system/status", () =>
        HttpResponse.json({
          ...CONNECTING_STATUS,
          last_wifi_attempt: { ssid: "Home Wi-Fi", result: "failed", timestamp: 1 },
        }),
      ),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    const button = await screen.findByRole("button", { name: "Try again" });
    expect(
      screen.getByText('The last connection attempt to "Home Wi-Fi" failed. Double-check the password and try again.'),
    ).toBeInTheDocument();

    button.click();
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("keeps waiting when the polled attempt is a stale record left over from a previous attempt", async () => {
    const onFailed = vi.fn();
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        // Same timestamp as previousAttemptTimestamp -- not strictly
        // greater, so this is the record that was already on disk before
        // this attempt's connect submitted, not this attempt's outcome.
        last_wifi_attempt: { ssid: "Home Wi-Fi", result: "failed", timestamp: 5 },
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        previousAttemptTimestamp={5}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    // Give the poll several ticks to prove it really never resolves, not
    // just that it has not resolved yet.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(onFailed).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("shows the failure screen once a fresh record (newer than previousAttemptTimestamp) reports failed", async () => {
    const onFailed = vi.fn();
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        last_wifi_attempt: { ssid: "Home Wi-Fi", result: "failed", timestamp: 6 },
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        previousAttemptTimestamp={5}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    const button = await screen.findByRole("button", { name: "Try again" });
    button.click();
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("shows the success screen once a fresh record reports connected while state is connected", async () => {
    const onConnected = vi.fn();
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        state: "connected",
        last_wifi_attempt: { ssid: "Home Wi-Fi", result: "connected", timestamp: 6 },
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={onConnected}
        onFailed={vi.fn()}
        previousAttemptTimestamp={5}
        pollIntervalMs={20}
        probeIntervalMs={50_000}
      />,
    );

    const button = await screen.findByRole("button", { name: "Go to dashboard" });
    button.click();
    expect(onConnected).toHaveBeenCalledTimes(1);
  });

  it("stops the probe and never calls assign once the poll has already resolved a failed outcome", async () => {
    const assign = vi.fn();
    let probeCallCount = 0;
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        last_wifi_attempt: { ssid: "Home Wi-Fi", result: "failed", timestamp: 1 },
      }),
      http.get("http://palmimo-1234.local/", () => {
        probeCallCount += 1;
        // Rejects once (fail-first), then would succeed on every later
        // probe -- but the poll above resolves "failed" first, so no probe
        // after that point may call `assign`.
        return probeCallCount === 1 ? HttpResponse.error() : HttpResponse.text("");
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={vi.fn()}
        pollIntervalMs={10}
        probeIntervalMs={10}
        assign={assign}
      />,
    );

    await screen.findByRole("button", { name: "Try again" });

    // Give the probe several more ticks' worth of real time to prove it
    // really stopped, not just that it hasn't fired yet.
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(assign).not.toHaveBeenCalled();
  });

  it("shows timeout guidance and stops polling once maxWaitMs elapses with no resolution", async () => {
    const onFailed = vi.fn();
    let statusCallCount = 0;
    server.use(
      http.get("*/api/v1/system/status", () => {
        statusCallCount += 1;
        return HttpResponse.json(CONNECTING_STATUS); // never resolves -- stays "connecting" forever
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        pollIntervalMs={10}
        probeIntervalMs={50_000}
        maxWaitMs={30}
      />,
    );

    expect(await screen.findByText("Still not connected")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Palmimo has not been found yet. If its own Wi-Fi network (“palmimo-1234”) reappeared, rejoin it and try again from this screen — or try opening http://palmimo-1234.local directly.",
      ),
    ).toBeInTheDocument();

    const callsAtTimeout = statusCallCount;
    // Give the poller several more ticks' worth of real time to prove it
    // really stopped (not just that it hasn't ticked again yet).
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(statusCallCount).toBeLessThanOrEqual(callsAtTimeout + 1);

    screen.getByRole("button", { name: "Back" }).click();
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("shows the connect error immediately (before maxWaitMs) when the POST failed before any attempt was recorded", async () => {
    const onFailed = vi.fn();
    server.use(getGetStatusApiV1SystemStatusGetMockHandler(CONNECTING_STATUS));
    const connectError = new PortalApiError(503, "network_backend_unavailable", {});

    renderWithProviders(
      <WifiWaitingPanel
        ssid="Home Wi-Fi"
        onConnected={vi.fn()}
        onFailed={onFailed}
        pollIntervalMs={50_000}
        probeIntervalMs={50_000}
        maxWaitMs={60_000}
        connectError={connectError}
      />,
    );

    expect(await screen.findByText("Palmimo could not reach its Wi-Fi service. Try again shortly.")).toBeInTheDocument();
    screen.getByRole("button", { name: "Back" }).click();
    expect(onFailed).toHaveBeenCalledTimes(1);
  });

  it("shows the different-known-network copy when state is connected and observed_connection_name differs from the requested ssid", async () => {
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        // comitup settled on -- and is CONNECTED to -- a different known
        // network than the one requested (the reconfigure-race resolution,
        // see core/wifi_attempt.py's resolve_attempt).
        state: "connected",
        last_wifi_attempt: {
          ssid: "Home Wi-Fi",
          result: "failed",
          timestamp: 1,
          observed_connection_name: "Neighbor's Wi-Fi",
        },
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel ssid="Home Wi-Fi" onConnected={vi.fn()} onFailed={vi.fn()} pollIntervalMs={20} probeIntervalMs={50_000} />,
    );

    expect(
      await screen.findByText(
        'Palmimo joined a different known network ("Neighbor\'s Wi-Fi") instead of "Home Wi-Fi". Choose "Home Wi-Fi" again if that\'s the one you want.',
      ),
    ).toBeInTheDocument();
  });

  it("shows the plain double-check-the-password copy, not the different-network copy, for a HOTSPOT-fallback failure", async () => {
    // A HOTSPOT-fallback failed attempt: the device landed back on its own
    // setup AP, not "connected" to anything. core/wifi_attempt.py's
    // resolve_attempt forces observed_connection_name to null for this
    // case, but this test sets the AP's own broadcast name directly to
    // prove the frontend's state === "connected" guard is what prevents
    // the wrong copy, not merely the backend omitting a name.
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler({
        ...CONNECTING_STATUS,
        state: "connecting",
        last_wifi_attempt: {
          ssid: "Home Wi-Fi",
          result: "failed",
          timestamp: 1,
          observed_connection_name: "palmimo-1234",
        },
      }),
    );

    renderWithProviders(
      <WifiWaitingPanel ssid="Home Wi-Fi" onConnected={vi.fn()} onFailed={vi.fn()} pollIntervalMs={20} probeIntervalMs={50_000} />,
    );

    expect(
      await screen.findByText('The last connection attempt to "Home Wi-Fi" failed. Double-check the password and try again.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/joined a different known network/)).not.toBeInTheDocument();
  });
});
