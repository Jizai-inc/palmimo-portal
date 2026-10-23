import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { describe, expect, it } from "vitest";

import type * as React from "react";

import { AppHeader } from "@/components/AppHeader";

/**
 * A minimal two-route tree (root + `/dashboard`) so the wordmark's `Link
 * to="/"` has somewhere to navigate from -- the full `routeTree.gen.ts`
 * pulls in every screen and its data fetching, which this test doesn't need.
 */
function renderAppHeaderAt(initialPath: string, wordmarkLinksHome: boolean, props: Partial<React.ComponentProps<typeof AppHeader>> = {}) {
  const rootRoute = createRootRoute({ component: () => <AppHeader wordmarkLinksHome={wordmarkLinksHome} {...props} /> });
  const dashboardRoute = createRoute({ getParentRoute: () => rootRoute, path: "/dashboard", component: () => null });
  const routeTree = rootRoute.addChildren([dashboardRoute]);
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [initialPath] }) });

  render(<RouterProvider router={router} />);

  return router;
}

describe("AppHeader", () => {
  it("renders the Palmimo DevKit wordmark as a link when wordmarkLinksHome is true", async () => {
    renderAppHeaderAt("/dashboard", true);

    expect(await screen.findByRole("link", { name: "Palmimo DevKit" })).toBeInTheDocument();
  });

  it("navigates to / when the linked wordmark is clicked", async () => {
    const router = renderAppHeaderAt("/dashboard", true);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "Palmimo DevKit" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("renders the Palmimo DevKit wordmark as an inert image when wordmarkLinksHome is false", async () => {
    renderAppHeaderAt("/dashboard", false);

    expect(await screen.findByRole("img", { name: "Palmimo DevKit" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Palmimo DevKit" })).not.toBeInTheDocument();
  });

  it("renders the actions slot", async () => {
    renderAppHeaderAt("/dashboard", true, { actions: <button type="button">Menu</button> });

    expect(await screen.findByRole("button", { name: "Menu" })).toBeInTheDocument();
  });

  it("links an available update to Settings → Software update", async () => {
    renderAppHeaderAt("/dashboard", true, { updateAvailable: true });

    expect(await screen.findByRole("link", { name: "Update available" })).toHaveAttribute("href", "/settings/update");
  });

  it("renders no update link when none is available", async () => {
    renderAppHeaderAt("/dashboard", true);

    await screen.findByRole("link", { name: "Palmimo DevKit" });
    expect(screen.queryByRole("link", { name: "Update available" })).not.toBeInTheDocument();
  });
});
