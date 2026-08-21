import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import {
  getConnectApiV1WifiConnectPostMockHandler,
  getGetStatusApiV1WifiStatusGetMockHandler,
  getListNetworksApiV1WifiNetworksGetMockHandler,
} from "@/api/generated/wifi/wifi.msw";
import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { queryClient } from "@/lib/queryClient";
// The real generated tree, not a hand-built stand-in (see AppHeader.test.tsx for that pattern
// elsewhere): only this mounts the actual file-route nesting the issue #13 regression is in --
// `routes/wifi_.waiting.tsx`'s file name is what keeps `/wifi/waiting` a sibling of `/wifi`
// rather than a child rendered into an `<Outlet/>` that `/wifi` (routes/wifi.tsx) never
// provides. Requires `routeTree.gen.ts` on disk (`make check`/`make build`/`npm run dev`
// produce it; a bare `npm test` on a from-scratch clone that skipped those does not). Kept out
// of `src/routes/` itself so the route-tree generator doesn't warn on a route-less file there
// (same reason as `lib/wifiWaitingGate.ts`).
import { routeTree } from "@/routeTree.gen";
import { server } from "@/test/server";

const SYSTEM_STATUS = {
  state: "disconnected",
  hostname: "palmimo-1234",
  auth_state: "set",
  device_id: "1234",
  versions: { portal: "0.1.0", sdk: null },
  last_wifi_attempt: null,
  adapters: "fake",
  state_dir: "/tmp",
};

describe("submit -> /wifi/waiting through the real route tree (issue #13)", () => {
  afterEach(() => {
    queryClient.clear();
  });

  it("paints the waiting screen promptly even though system/status hangs from the moment of submit", async () => {
    // Mirrors the real failure mode: system/status answers normally until the connect POST
    // fires, then hangs forever -- the AP the browser is talking through is gone. Nothing here
    // uses fake timers: if either the nesting regresses or the root gate's skip for
    // `/wifi/waiting` regresses, this test hangs instead of merely going slow.
    let tornDown = false;
    server.use(
      getGetStatusApiV1SystemStatusGetMockHandler(() => (tornDown ? new Promise<never>(() => {}) : SYSTEM_STATUS)),
      getGetStatusApiV1WifiStatusGetMockHandler({ ssid: null, ip_address: null, state: "disconnected" }),
      getListNetworksApiV1WifiNetworksGetMockHandler([{ ssid: "Home-5G", signal: 80, secured: false }]),
      getConnectApiV1WifiConnectPostMockHandler(() => {
        tornDown = true;
        return new Promise<never>(() => {});
      }),
    );

    const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/wifi"] }) });
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /Home-5G/ }));
    await user.click(await screen.findByRole("button", { name: "Connect" }));

    expect(await screen.findByRole("heading", { name: "Connecting…" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/wifi/waiting");
  });
});
