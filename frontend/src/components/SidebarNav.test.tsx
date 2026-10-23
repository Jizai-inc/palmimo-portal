import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { SidebarNav } from "@/components/SidebarNav";
import { NAV_ITEMS, SETTINGS_ITEMS } from "@/lib/navigation";

function renderNavAt(path: string, { rail = false, updateAvailable = false } = {}) {
  const rootRoute = createRootRoute({
    component: () => <SidebarNav rail={rail} updateAvailable={updateAvailable} idPrefix="test" />,
  });
  const leaves = [...NAV_ITEMS, ...SETTINGS_ITEMS].map((item) =>
    createRoute({ getParentRoute: () => rootRoute, path: item.to, component: () => null }),
  );
  const router = createRouter({ routeTree: rootRoute.addChildren(leaves), history: createMemoryHistory({ initialEntries: [path] }) });
  return { router, ...render(<RouterProvider router={router} />) };
}

const settingsToggle = () => screen.getByRole("button", { name: /^Settings/ });

describe("SidebarNav settings group", () => {
  beforeEach(() => localStorage.clear());

  it("expands on a settings route and marks only the current section as the page", async () => {
    renderNavAt("/settings/device");

    const nav = await screen.findByRole("navigation", { name: "Primary navigation" });
    expect(settingsToggle()).toHaveAttribute("aria-expanded", "true");
    const current = within(nav).getAllByRole("link").filter((link) => link.getAttribute("aria-current") === "page");
    expect(current.map((link) => link.textContent)).toEqual(["Device"]);
  });

  it("is collapsed by default elsewhere, with Home as the only current page", async () => {
    renderNavAt("/dashboard");

    await screen.findByRole("navigation", { name: "Primary navigation" });
    expect(settingsToggle()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: "Network" })).not.toBeInTheDocument();
    const current = screen.getAllByRole("link").filter((link) => link.getAttribute("aria-current") === "page");
    expect(current.map((link) => link.textContent)).toEqual(["Home"]);
  });

  it("opens and closes the group from the Settings row without navigating", async () => {
    const user = userEvent.setup();
    const { router } = renderNavAt("/dashboard");

    await user.click(await screen.findByRole("button", { name: "Settings" }));

    expect(settingsToggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Network" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/dashboard");

    await user.click(settingsToggle());

    expect(settingsToggle()).toHaveAttribute("aria-expanded", "false");
  });

  it("toggles from the keyboard", async () => {
    const user = userEvent.setup();
    renderNavAt("/dashboard");
    (await screen.findByRole("button", { name: "Settings" })).focus();

    await user.keyboard("{Enter}");

    expect(settingsToggle()).toHaveAttribute("aria-expanded", "true");
  });

  it("points the toggle at the list it controls", async () => {
    renderNavAt("/settings/network");

    await screen.findByRole("navigation", { name: "Primary navigation" });
    const list = document.getElementById(settingsToggle().getAttribute("aria-controls") ?? "");
    expect(list).not.toBeNull();
    expect(within(list as HTMLElement).getAllByRole("link")).toHaveLength(SETTINGS_ITEMS.length);
  });

  it("remembers an expand choice across page loads", async () => {
    const user = userEvent.setup();
    const { unmount } = renderNavAt("/dashboard");
    await user.click(await screen.findByRole("button", { name: "Settings" }));
    unmount();

    renderNavAt("/apps");

    await screen.findByRole("navigation", { name: "Primary navigation" });
    expect(settingsToggle()).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the update dot on Software update when expanded, and on Settings when collapsed", async () => {
    const user = userEvent.setup();
    renderNavAt("/settings/network", { updateAvailable: true });

    expect(await screen.findByRole("link", { name: /Software update/ })).toHaveTextContent("Update available");
    expect(settingsToggle()).not.toHaveTextContent("Update available");

    await user.click(settingsToggle());

    expect(settingsToggle()).toHaveTextContent("Update available");
  });
});

describe("SidebarNav rail", () => {
  it("hides the sections and marks the Settings icon as the page on any settings route", async () => {
    renderNavAt("/settings/developer", { rail: true });

    const settings = await screen.findByRole("link", { name: "Settings" });
    expect(settings).toHaveAttribute("aria-current", "page");
    expect(settings).toHaveAttribute("href", "/settings/network");
    expect(screen.queryByRole("link", { name: "Developer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Settings/ })).not.toBeInTheDocument();
  });
});
