import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getGetCatalogApiV1CatalogGetMockHandler } from "@/api/generated/catalog/catalog.msw";
import { getGetPlatformApiV1PlatformGetMockHandler } from "@/api/generated/platform/platform.msw";
import { getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1WifiStatusGetMockHandler } from "@/api/generated/wifi/wifi.msw";
import { queryClient } from "@/lib/queryClient";
// The real generated tree (see wifiConnectToWaiting.test.tsx): only it carries the file-route
// nesting. `/apps` renders no `<Outlet/>`, so a child named `apps.add.tsx` would match the URL
// and paint nothing new.
import { routeTree } from "@/routeTree.gen";
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
};

describe("apps child routes through the real route tree", () => {
  afterEach(() => {
    queryClient.clear();
  });

  it("opens the add-app screen from the apps list", async () => {
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(SYSTEM_STATUS),
      getGetStatusApiV1WifiStatusGetMockHandler({ ssid: "Home", ip_address: null, state: "connected" }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getGetPlatformApiV1PlatformGetMockHandler(),
      getGetCatalogApiV1CatalogGetMockHandler({ tag: null, fetched_at: null, stale: false, reason: null, apps: [] }),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/apps"] }) });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "Add app" }));

    expect(await screen.findByRole("heading", { name: "Add app" })).toBeInTheDocument();
  });
});
