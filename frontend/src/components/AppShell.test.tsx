import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import type { UpdateStatusResponse } from "@/api/generated/models";
import { getGetStatusApiV1SystemStatusGetMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1UpdateStatusGetMockHandler } from "@/api/generated/update/update.msw";
import { getGetStatusApiV1WifiStatusGetMockHandler } from "@/api/generated/wifi/wifi.msw";
import { AppShell } from "@/components/AppShell";
import { NAV_ITEMS } from "@/lib/navigation";
import { stubMatchMedia } from "@/test/matchMedia";
import { server } from "@/test/server";

const IDLE_JOB = {
  kind: "update",
  state: "idle",
  target: null,
  step: null,
  error: null,
  started_at: null,
  finished_at: null,
  restarting_at: null,
};

const BASE_UPDATE_STATUS: UpdateStatusResponse = {
  installed: { tag: "v1.0.0", commit: "abc1234" },
  latest: { tag: "v1.0.0", name: "v1.0.0", published_at: "2026-01-01T00:00:00Z", html_url: "https://example.test/v1" },
  checked_at: Math.floor(Date.now() / 1000),
  update_available: false,
  previous_tag: null,
  retry_available: false,
  job: IDLE_JOB,
};

/** Registers the three status endpoints `AppShell` reads; `update` defaults to no update available unless overridden. */
function stubStatuses({ update }: { update?: UpdateStatusResponse } = {}) {
  server.use(
    getGetStatusApiV1WifiStatusGetMockHandler(),
    getGetStatusApiV1SystemStatusGetMockHandler(),
    getGetStatusApiV1UpdateStatusGetMockHandler(update ?? BASE_UPDATE_STATUS),
  );
}

/** Fails `update/status` outright, so `AppShell` renders as if no status were known yet. */
function stubUpdateStatusError() {
  server.use(
    getGetStatusApiV1WifiStatusGetMockHandler(),
    getGetStatusApiV1SystemStatusGetMockHandler(),
    http.get("*/api/v1/update/status", () => HttpResponse.json({ error: { code: "internal_error", params: {} } }, { status: 500 })),
  );
}

/**
 * A synthetic route tree that mounts `AppShell` at `/dashboard` with every other `NAV_ITEMS`
 * path present as a no-op leaf, so `<Link>` targets resolve without pulling in the app's real
 * screens (loaders, auth guard, network calls) that a route-tree-based render would trigger.
 */
function renderAppShell() {
  const rootRoute = createRootRoute();
  const shellRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/dashboard",
    component: () => (
      <AppShell title="Test">
        <p>content</p>
      </AppShell>
    ),
  });
  const otherRoutes = NAV_ITEMS.filter((item) => item.to !== "/dashboard").map((item) =>
    createRoute({ getParentRoute: () => rootRoute, path: item.to, component: () => null }),
  );
  const routeTree = rootRoute.addChildren([shellRoute, ...otherRoutes]);
  const router = createRouter({ routeTree, history: createMemoryHistory({ initialEntries: ["/dashboard"] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

/** The header's nav toggle -- collapses the sidebar on desktop, opens the drawer on mobile (see AppShell.tsx). */
function toggleButton() {
  return screen.getByRole("button", { name: "Toggle sidebar" });
}

describe("AppShell mobile drawer", () => {
  it("is closed by default, with no drawer dialog in the document", async () => {
    stubStatuses();
    renderAppShell();

    await screen.findAllByText("Home");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens from the header toggle and lists every NAV_ITEMS entry", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());

    const dialog = await screen.findByRole("dialog", { name: "Primary navigation" });
    for (const label of ["Home", "Wi-Fi", "SSH keys", "Power", "Update"]) {
      expect(within(dialog).getByText(label)).toBeInTheDocument();
    }
  });

  it("closes when a drawer link is clicked", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    const dialog = await screen.findByRole("dialog", { name: "Primary navigation" });
    await user.click(within(dialog).getByText("Power"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("closes on Escape and restores focus to the toggle button", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    await screen.findByRole("dialog", { name: "Primary navigation" });

    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(toggleButton()).toHaveFocus();
  });

  it("closes when the backdrop is clicked", async () => {
    const user = userEvent.setup();
    stubStatuses();
    const { container } = renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    await screen.findByRole("dialog", { name: "Primary navigation" });
    const backdrop = container.querySelector('[data-testid="drawer-backdrop"]');
    expect(backdrop).not.toBeNull();
    await user.click(backdrop as Element);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("closes via the drawer's own close button", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    const dialog = await screen.findByRole("dialog", { name: "Primary navigation" });
    await user.click(within(dialog).getByRole("button", { name: "Close menu" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("locks body scroll while open and restores it on close", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");
    expect(document.body.style.overflow).not.toBe("hidden");

    await user.click(toggleButton());
    await screen.findByRole("dialog", { name: "Primary navigation" });
    expect(document.body.style.overflow).toBe("hidden");

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(document.body.style.overflow).not.toBe("hidden");
  });

  it("traps Tab within the drawer, wrapping from the last link to the close button", async () => {
    const user = userEvent.setup();
    stubStatuses();
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    const dialog = await screen.findByRole("dialog", { name: "Primary navigation" });
    const closeButton = within(dialog).getByRole("button", { name: "Close menu" });
    const links = within(dialog).getAllByRole("link");
    links[links.length - 1].focus();

    await user.tab();

    expect(closeButton).toHaveFocus();
  });

  it("closes the drawer when the viewport crosses into desktop width", async () => {
    const user = userEvent.setup();
    stubStatuses();
    const mediaQuery = stubMatchMedia(false);
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    await screen.findByRole("dialog", { name: "Primary navigation" });

    mediaQuery.setMatches("(min-width: 768px)", true);

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});

describe("AppShell desktop sidebar", () => {
  it("collapses the sidebar from the header toggle without opening the drawer", async () => {
    const user = userEvent.setup();
    stubStatuses();
    stubMatchMedia(true);
    const { container } = renderAppShell();
    await screen.findAllByText("Home");
    expect(container.querySelector("aside")).not.toBeNull();

    await user.click(toggleButton());

    expect(container.querySelector("aside")).toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("never attaches the drawer's document-level Escape/Tab listener at desktop width", async () => {
    const user = userEvent.setup();
    stubStatuses();
    stubMatchMedia(true);
    const addEventListenerSpy = vi.spyOn(document, "addEventListener");
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());

    expect(addEventListenerSpy).not.toHaveBeenCalledWith("keydown", expect.any(Function));
    addEventListenerSpy.mockRestore();
  });
});

describe("AppShell update badge", () => {
  it("renders an accessible update-available announcement when the update is available", async () => {
    stubStatuses({ update: { ...BASE_UPDATE_STATUS, update_available: true } });
    renderAppShell();

    await screen.findAllByText("Home");
    expect(await screen.findAllByText("Update available")).not.toHaveLength(0);
  });

  it("renders no update-available announcement when already up to date", async () => {
    stubStatuses({ update: { ...BASE_UPDATE_STATUS, update_available: false } });
    renderAppShell();

    await screen.findAllByText("Home");
    expect(screen.queryAllByText("Update available")).toHaveLength(0);
  });

  it("renders no update-available announcement while update/status errors", async () => {
    stubUpdateStatusError();
    renderAppShell();

    await screen.findAllByText("Home");
    expect(screen.queryAllByText("Update available")).toHaveLength(0);
  });

  it("never calls update/check on mount or on drawer interactions", async () => {
    const user = userEvent.setup();
    const checkSpy = vi.fn();
    server.use(http.post("*/api/v1/update/check", () => (checkSpy(), HttpResponse.json(BASE_UPDATE_STATUS))));
    stubStatuses({ update: { ...BASE_UPDATE_STATUS, update_available: true } });
    renderAppShell();
    await screen.findAllByText("Home");

    await user.click(toggleButton());
    await screen.findByRole("dialog", { name: "Primary navigation" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    expect(checkSpy).not.toHaveBeenCalled();
  });
});
