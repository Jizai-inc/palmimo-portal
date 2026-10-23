import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { RunningAppInfo } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetQueryKey } from "@/api/generated/system/system";
import { RunningAppControl } from "@/components/RunningAppControl";
import { server } from "@/test/server";

function renderControl(runningApp: RunningAppInfo | null, defaultApp: { id: string; name: string } | null = null) {
  const rootRoute = createRootRoute({ component: () => <RunningAppControl runningApp={runningApp} defaultApp={defaultApp} /> });
  const leaves = ["/home", "/apps", "/apps/$id"].map((path) =>
    createRoute({ getParentRoute: () => rootRoute, path, component: () => null }),
  );
  const router = createRouter({
    routeTree: rootRoute.addChildren(leaves),
    history: createMemoryHistory({ initialEntries: ["/home"] }),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { router, invalidateSpy };
}

describe("RunningAppControl", () => {
  it("links to Apps when no app is running", async () => {
    const user = userEvent.setup();
    const { router } = renderControl(null);

    await user.click(await screen.findByRole("link", { name: /No app running/ }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/apps"));
  });

  it("links the running app's name to its detail page", async () => {
    renderControl({ id: "robot", name: "Hello Robot", state: "running" });

    expect(await screen.findByRole("link", { name: "Running app: Hello Robot" })).toHaveAttribute("href", "/apps/robot");
  });

  it("stops the running app and refreshes system status", async () => {
    const user = userEvent.setup();
    const stopSpy = vi.fn();
    server.use(
      http.post("*/api/v1/apps/robot/stop", () => (stopSpy(), HttpResponse.json({ status: "stopping" }, { status: 202 }))),
    );
    const { invalidateSpy } = renderControl({ id: "robot", name: "Hello Robot", state: "running" });

    await user.click(await screen.findByRole("button", { name: "Stop" }));

    await waitFor(() => expect(stopSpy).toHaveBeenCalledOnce());
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: getGetStatusApiV1SystemStatusGetQueryKey() }),
    );
  });

  it("disables Stop while the app is already stopping", async () => {
    renderControl({ id: "robot", name: "Hello Robot", state: "stopping" });

    expect(await screen.findByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("announces a failed stop", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/robot/stop", () =>
        HttpResponse.json({ error: { code: "app_busy", params: {} } }, { status: 409 }),
      ),
    );
    renderControl({ id: "robot", name: "Hello Robot", state: "running" });

    await user.click(await screen.findByRole("button", { name: "Stop" }));

    expect(await screen.findByRole("alert")).not.toBeEmptyDOMElement();
  });

  it("offers to start the default app when nothing runs", async () => {
    const user = userEvent.setup();
    const startSpy = vi.fn();
    server.use(
      http.post("*/api/v1/apps/robot/start", () => (startSpy(), HttpResponse.json({ status: "activating" }, { status: 202 }))),
    );
    renderControl(null, { id: "robot", name: "Hello Robot" });

    await user.click(await screen.findByRole("button", { name: "Start Hello Robot" }));

    await waitFor(() => expect(startSpy).toHaveBeenCalledOnce());
    expect(screen.queryByText("No app running")).not.toBeInTheDocument();
  });
});
