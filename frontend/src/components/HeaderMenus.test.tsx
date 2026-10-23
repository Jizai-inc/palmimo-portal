import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { AccountMenu, DeviceMenu } from "@/components/HeaderMenus";
import { server } from "@/test/server";

function renderMenus() {
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <DeviceMenu hostname="palmimo-1234" network="Home · 192.168.1.20" connected />
        <AccountMenu />
      </>
    ),
  });
  const leaves = ["/dashboard", "/settings/device", "/login"].map((path) =>
    createRoute({ getParentRoute: () => rootRoute, path, component: () => null }),
  );
  const router = createRouter({
    routeTree: rootRoute.addChildren(leaves),
    history: createMemoryHistory({ initialEntries: ["/dashboard"] }),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClient.setQueryData(["sentinel"], "cached");

  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, queryClient };
}

describe("DeviceMenu", () => {
  it("shows the device's network in the open menu", async () => {
    const user = userEvent.setup();
    renderMenus();

    await user.click(await screen.findByRole("button", { name: /palmimo-1234/ }));

    expect(await screen.findByText("Home · 192.168.1.20")).toBeInTheDocument();
  });

  it.each([
    ["Restart", "restart"],
    ["Shut down", "shutdown"],
  ])("%s navigates to Settings → Device with action=%s", async (label, action) => {
    const user = userEvent.setup();
    const { router } = renderMenus();

    await user.click(await screen.findByRole("button", { name: /palmimo-1234/ }));
    await user.click(await screen.findByRole("menuitem", { name: label }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/settings/device"));
    expect(router.state.location.search).toEqual({ action });
  });
});

describe("AccountMenu", () => {
  it("logs out, clears the query cache, and goes to /login", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/logout", () => HttpResponse.json({ status: "ok" })));
    const { router, queryClient } = renderMenus();

    await user.click(await screen.findByRole("button", { name: "Account" }));
    await user.click(await screen.findByRole("menuitem", { name: "Log out" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(queryClient.getQueryData(["sentinel"])).toBeUndefined();
  });
});
