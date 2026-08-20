import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryHistory, createRootRoute, createRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import { describe, expect, it } from "vitest";

import { AppHeader } from "@/components/AppHeader";

/**
 * A minimal two-route tree (root + `/dashboard`) so the wordmark's `Link
 * to="/"` has somewhere to navigate from -- the full `routeTree.gen.ts`
 * pulls in every screen and its data fetching, which this test doesn't need.
 */
function renderAppHeaderAt(initialPath: string) {
  const rootRoute = createRootRoute({ component: () => <AppHeader /> });
  const dashboardRoute = createRoute({ getParentRoute: () => rootRoute, path: "/dashboard", component: () => null });
  const routeTree = rootRoute.addChildren([dashboardRoute]);
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: [initialPath] }) });

  render(<RouterProvider router={router} />);

  return router;
}

describe("AppHeader", () => {
  it("renders the Palmimo DevKit wordmark", async () => {
    renderAppHeaderAt("/dashboard");

    expect(await screen.findByRole("link", { name: "Palmimo DevKit" })).toBeInTheDocument();
  });

  it("navigates to / when the wordmark is clicked", async () => {
    const router = renderAppHeaderAt("/dashboard");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("link", { name: "Palmimo DevKit" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });
});
