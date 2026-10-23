import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import type { AppSummary } from "@/api/generated/models";
import { getGetPlatformApiV1PlatformGetMockHandler, getGetPlatformApiV1PlatformGetResponseMock } from "@/api/generated/platform/platform.msw";
import { getListGitCredentialsApiV1GitCredentialsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import { getGetChecklistApiV1SystemChecklistGetMockHandler, getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1UpdateStatusGetMockHandler, getGetStatusApiV1UpdateStatusGetResponseMock } from "@/api/generated/update/update.msw";
import { getGetStatusApiV1WifiStatusGetMockHandler } from "@/api/generated/wifi/wifi.msw";
import { queryClient } from "@/lib/queryClient";
import { routeTree } from "@/routeTree.gen";
import { appSummary } from "@/test/appSummaryFixture";
import { server } from "@/test/server";

const SYSTEM_STATUS = {
  state: "connected",
  hostname: "palmimo-1234",
  auth_state: "set",
  device_id: "1234",
  versions: { portal: "0.1.0", sdk: null },
  last_wifi_attempt: null,
  adapters: "fake",
  state_dir: "/tmp",
  disk_free_bytes: 5_000_000_000,
  ntp_synchronized: true,
  running_app: null,
};

function stubHome({ apps = [], dismissed = false }: { apps?: AppSummary[]; dismissed?: boolean } = {}) {
  server.use(
    getGetStatusApiV1SystemStatusGetMockHandler(SYSTEM_STATUS),
    getGetStatusApiV1WifiStatusGetMockHandler({ ssid: "Home", ip_address: null, state: "connected" }),
    getGetStatusApiV1UpdateStatusGetMockHandler(getGetStatusApiV1UpdateStatusGetResponseMock({ update_available: false })),
    getGetPlatformApiV1PlatformGetMockHandler(getGetPlatformApiV1PlatformGetResponseMock({ ready: true })),
    getListAppsApiV1AppsGetMockHandler({ apps }),
    getListGitCredentialsApiV1GitCredentialsGetMockHandler({ credentials: [] }),
    getGetChecklistApiV1SystemChecklistGetMockHandler({ dismissed }),
  );
}

function renderAt(path: string) {
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [path] }) });
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);
  return router;
}

describe("Home", () => {
  afterEach(() => {
    queryClient.clear();
  });

  it("sends the old /dashboard URL to /home", async () => {
    stubHome();
    const router = renderAt("/dashboard");

    expect(await screen.findByRole("heading", { name: "Home", level: 1 })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/home");
  });

  it("shows the setup checklist with progress and hides it on request", async () => {
    const putSpy = vi.fn();
    stubHome();
    server.use(
      http.put("*/api/v1/system/checklist", async ({ request }) => {
        putSpy(await request.json());
        return HttpResponse.json({ dismissed: true });
      }),
    );
    renderAt("/home");
    const user = userEvent.setup();

    expect(await screen.findByText("0 of 3 done")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Read the getting started guide" })).toHaveAttribute(
      "href",
      "https://docs.palmimo.dev/guides/installation/",
    );
    await user.click(screen.getByRole("button", { name: "Hide" }));

    await waitFor(() => expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument());
    expect(putSpy).toHaveBeenCalledWith({ dismissed: true });
  });

  it("does not show a dismissed checklist", async () => {
    stubHome({ dismissed: true });
    renderAt("/home");

    expect(await screen.findByRole("heading", { name: "Recent activity" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Get started" })).not.toBeInTheDocument();
  });

  it("shows needs attention only when something needs it", async () => {
    stubHome({ apps: [appSummary({ name: "robot", broken_reason: "venv_missing" })] });
    renderAt("/home");

    expect(await screen.findByRole("link", { name: /robot needs repair/ })).toHaveAttribute("href", "/apps/alice.robot");
  });

  it("omits needs attention when nothing needs it", async () => {
    stubHome({ apps: [appSummary()] });
    renderAt("/home");

    expect(await screen.findByRole("heading", { name: "Recent activity" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Needs attention" })).not.toBeInTheDocument();
  });
});
