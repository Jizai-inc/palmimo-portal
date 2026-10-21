import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { getGetPlatformApiV1PlatformGetMockHandler } from "@/api/generated/platform/platform.msw";
import { SavedNetworksCard } from "@/components/SavedNetworksCard";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const SAVED = [
  { ssid: "HomeNet", active: true },
  { ssid: "Phone", active: false },
  { ssid: "Cafe", active: false },
];


function platformAt(installed_version: number) {
  return getGetPlatformApiV1PlatformGetMockHandler({
    installed_at: null,
    installed_version,
    latest: null,
    latest_error: null,
    ready: true,
    reason: null,
    required_version: 1,
    verify_diffs: [],
  });
}

function stubSaved(list = SAVED) {
  server.use(platformAt(4), http.get("*/api/v1/wifi/saved", () => HttpResponse.json(list)));
}

function rowNames() {
  return within(screen.getByRole("list"))
    .getAllByRole("listitem")
    .map((item) => item.textContent ?? "");
}

describe("SavedNetworksCard", () => {
  it("lists the saved networks in order, marking the current one and hiding its delete button", async () => {
    stubSaved();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={vi.fn()} onSwitched={vi.fn()} />);

    await screen.findByText("Phone");
    const rows = rowNames();
    expect(rows[0]).toContain("HomeNet");
    expect(rows[0]).toContain("Connected");
    expect(rows[1]).toContain("Phone");
    expect(rows[2]).toContain("Cafe");
    expect(screen.getByRole("button", { name: "Delete HomeNet" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move HomeNet up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move Cafe down" })).toBeDisabled();
  });

  it("moving a network down sends the new order and shows it at once", async () => {
    stubSaved();
    let sent: unknown = null;
    server.use(
      http.put("*/api/v1/wifi/saved/order", async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json([SAVED[1], SAVED[0], SAVED[2]]);
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={vi.fn()} onSwitched={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Move HomeNet down" }));

    expect(rowNames()[0]).toContain("Phone");
    await waitFor(() => expect(sent).toEqual({ ssids: ["Phone", "HomeNet", "Cafe"] }));
  });

  it("delete: dialog -> confirm -> DELETE called -> the row disappears", async () => {
    stubSaved();
    let deleted = "";
    server.use(
      http.post("*/api/v1/wifi/saved/delete", async ({ request }) => {
        deleted = ((await request.json()) as { ssid: string }).ssid;
        return HttpResponse.json([SAVED[0], SAVED[1]]);
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={vi.fn()} onSwitched={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Delete Cafe" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(screen.queryByText("Cafe")).not.toBeInTheDocument());
    expect(deleted).toBe("Cafe");
  });

  it("switch: dialog -> confirm -> activate called -> onSwitched with the ssid", async () => {
    stubSaved();
    let activated = "";
    server.use(
      http.post("*/api/v1/wifi/saved/activate", async ({ request }) => {
        activated = ((await request.json()) as { ssid: string }).ssid;
        return HttpResponse.json({ status: "switching" });
      }),
    );
    const onSwitched = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={vi.fn()} onSwitched={onSwitched} />);

    const phoneRow = (await screen.findByText("Phone")).closest("li");
    expect(phoneRow).not.toBeNull();
    await user.click(within(phoneRow as HTMLElement).getByRole("button", { name: "Switch" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/HomeNet stays saved/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Switch" }));

    await waitFor(() => expect(onSwitched).toHaveBeenCalledWith("Phone"));
    expect(activated).toBe("Phone");
  });

  it("asks for a platform update instead of listing when the platform is older than 3", async () => {
    let listed = false;
    let updateStarted = false;
    server.use(
      platformAt(2),
      http.get("*/api/v1/wifi/saved", () => {
        listed = true;
        return HttpResponse.json(SAVED);
      }),
      http.post("*/api/v1/platform/update", () => {
        updateStarted = true;
        return HttpResponse.json({ job: { state: "running" } }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={vi.fn()} onSwitched={vi.fn()} />);

    expect(await screen.findByText("Update the device platform to save several networks")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Update now" }));

    await waitFor(() => expect(updateStarted).toBe(true));
    expect(listed).toBe(false);
    expect(screen.queryByText("Phone")).not.toBeInTheDocument();
  });

  it("the add button calls onAdd", async () => {
    stubSaved();
    const onAdd = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<SavedNetworksCard hostname="palmimo-1234" onAdd={onAdd} onSwitched={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Add" }));

    expect(onAdd).toHaveBeenCalledOnce();
  });
});
