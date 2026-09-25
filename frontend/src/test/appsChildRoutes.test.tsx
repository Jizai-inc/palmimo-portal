import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { getGetAppApiV1AppsNameGetMockHandler, getGetLogsApiV1AppsNameLogsGetMockHandler, getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getGetCatalogApiV1CatalogGetMockHandler } from "@/api/generated/catalog/catalog.msw";
import type { AppDetailResponse } from "@/api/generated/models";
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

  // "Close" could strand the operator on the full logs page instead of returning to the app.
  it("expands to the full logs page from the detail screen and closes back to it", async () => {
    const app: AppDetailResponse = {
      autostart: false,
      bindings: {},
      broken_reason: null,
      description: "A test app.",
      devices: [],
      env: [],
      installed_at: 1_700_000_000,
      last_job: null,
      manifest: { devices: [], env: [], params: [] },
      id: "palmimo.teleop",
      name: "palmimo-teleop",
      params: {},
      source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc123" },
      status: "running",
    };
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(SYSTEM_STATUS),
      getGetStatusApiV1WifiStatusGetMockHandler({ ssid: "Home", ip_address: null, state: "connected" }),
      getGetAppApiV1AppsNameGetMockHandler(app),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getGetLogsApiV1AppsNameLogsGetMockHandler({
        entries: [{ message: "server started", timestamp: 1_700_000_000, invocation_id: "11111111111111111111111111111111" }],
        invocations: [],
        next_cursor: null,
      }),
    );
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/apps/palmimo.teleop"] }) });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "Expand" }));

    expect(await screen.findByRole("heading", { name: "palmimo-teleop logs" })).toBeInTheDocument();
    expect(await screen.findByText("server started")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Close" }));

    expect(await screen.findByRole("button", { name: "Delete this app" })).toBeInTheDocument();
  });

  // direct navigation or a reload -- only a route parameter named `$id` (not `$name`) routes a
  // URL segment containing a `.` to the detail screen at all.
  it("opens the detail screen directly at a dotted app id, as a fresh navigation would see on reload", async () => {
    const app: AppDetailResponse = {
      autostart: false, bindings: {}, broken_reason: null, description: "A test app.", devices: [], env: [], installed_at: 1_700_000_000,
      last_job: null, manifest: { devices: [], env: [], params: [] }, id: "alice.teleop-fork", name: "teleop", params: {},
      source: { type: "git", official: false, url: "https://github.com/alice/teleop", subdir: null, manifest: null, ref_kind: "branch", ref: "main", commit: "abc123" },
      status: "stopped",
    };
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(SYSTEM_STATUS),
      getGetStatusApiV1WifiStatusGetMockHandler({ ssid: "Home", ip_address: null, state: "connected" }),
      getGetAppApiV1AppsNameGetMockHandler(app),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }),
    );
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/apps/alice.teleop-fork"] }) });
    render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);

    expect(await screen.findByRole("button", { name: "Delete this app" })).toBeInTheDocument();
    expect(screen.getByText("alice.")).toBeInTheDocument();
    expect(screen.getByText("teleop-fork")).toBeInTheDocument();
  });

  it("returns to the apps list from an app detail breadcrumb", async () => {
    const app: AppDetailResponse = {
      autostart: false, bindings: {}, broken_reason: null, description: "A test app.", devices: [], env: [], installed_at: 1_700_000_000,
      last_job: null, manifest: { devices: [], env: [], params: [] }, id: "palmimo.teleop", name: "palmimo-teleop", params: {},
      source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc123" }, status: "stopped",
    };
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(SYSTEM_STATUS),
      getGetStatusApiV1WifiStatusGetMockHandler({ ssid: "Home", ip_address: null, state: "connected" }),
      getGetAppApiV1AppsNameGetMockHandler(app),
      getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getGetPlatformApiV1PlatformGetMockHandler(),
    );
    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/apps/palmimo.teleop"] }) });
    render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>);
    const user = userEvent.setup();

    await user.click(within(await screen.findByRole("navigation", { name: "Breadcrumb" })).getByRole("link", { name: "Apps" }));

    expect(await screen.findByText("No apps installed yet.")).toBeInTheDocument();
  });
});
