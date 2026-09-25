import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getGetPlatformApiV1PlatformGetMockHandler } from "@/api/generated/platform/platform.msw";
import type { AppSummary, PlatformStatusResponse } from "@/api/generated/models";
import { AppsListPanel } from "@/components/AppsListPanel";
import { renderWithRouter } from "@/test/render";
import { server } from "@/test/server";

const READY_PLATFORM: PlatformStatusResponse = {
  installed_at: null,
  installed_version: 1,
  latest: null,
  latest_error: null,
  ready: true,
  reason: null,
  required_version: 1,
  verify_diffs: [],
};

function app(overrides: Partial<AppSummary>): AppSummary {
  const name = overrides.name ?? "palmimo-teleop";
  return {
    autostart: false,
    broken_reason: null,
    credential_rejected: false,
    id: `zip.${name}`,
    installed_at: 1_700_000_000,
    last_job: null,
    latest_commit: null,
    name,
    source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc" },
    status: "stopped",
    update_available: false,
    ...overrides,
  };
}

describe("AppsListPanel", () => {
  it.each([
    ["running", ["Stop"]],
    ["stopped", ["Start"]],
    ["failed", ["Start"]],
    ["needs_repair", ["Repair"]],
    ["broken", ["Repair"]],
  ] as const)("shows the %s status label and its action button", async (status, expectedButtons) => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({ apps: [app({ status })] }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    await screen.findByText("palmimo-teleop");
    for (const label of expectedButtons) {
      expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("shows an Open button only when a running app has a url", async () => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({ apps: [app({ status: "running", url: "http://palmimo.local:8765" })] }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    const openLink = await screen.findByRole("link", { name: "Open" });
    expect(openLink).toHaveAttribute("href", "http://palmimo.local:8765");
  });

  it("shows the autostart and update-available badges", async () => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({ apps: [app({ autostart: true, update_available: true })] }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    await screen.findByText("palmimo-teleop");
    expect(screen.getByText("Autostart")).toBeInTheDocument();
    expect(screen.getByText("Update available")).toBeInTheDocument();
  });

  // Without this, an operator would have no way to tell from the apps list that an app's git
  // credential was rejected (e.g. revoked token) and needs re-registering before it can update.
  it("shows a rejected-credential badge only on the app whose credential was rejected", async () => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({
        apps: [app({ name: "palmimo-teleop", credential_rejected: true }), app({ name: "palmimo-companion", credential_rejected: false })],
      }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    await screen.findByText("palmimo-teleop");
    const rejectedRow = screen.getByText("palmimo-teleop").closest("li");
    const okRow = screen.getByText("palmimo-companion").closest("li");
    expect(rejectedRow && within(rejectedRow).getByText("Credential rejected")).toBeInTheDocument();
    expect(okRow && within(okRow).queryByText("Credential rejected")).not.toBeInTheDocument();
  });

  // Without this, two apps installed from different sources under the same manifest `name`
  // (design doc 3.9's "same owner, two branches" case) would be indistinguishable in the list
  // -- same visible label, no way to tell which row starts which app.
  it("shows two apps with the same manifest name as separate rows distinguished by id and source", async () => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({
        apps: [
          app({
            name: "teleop",
            id: "palmimo.teleop",
            source: { type: "git", official: true, url: "https://github.com/Jizai-inc/palmimo-devkit", subdir: "examples/teleop", manifest: null, ref_kind: "tag", ref: "v1", commit: "abc" },
          }),
          app({
            name: "teleop",
            id: "alice.teleop",
            source: { type: "git", official: false, url: "https://github.com/alice/teleop", subdir: null, manifest: null, ref_kind: "branch", ref: "main", commit: "def" },
          }),
        ],
      }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    const rows = (await screen.findAllByText("teleop", { selector: "span" })).map((node) => node.closest("li"));
    expect(rows).toHaveLength(2);
    const officialRow = rows.find((row) => row && within(row).queryByRole("link", { name: /palmimo\.teleop/ }));
    expect(officialRow && within(officialRow).getByRole("link", { name: /palmimo\.teleop/ })).toBeInTheDocument();
    expect(officialRow && within(officialRow).getByText("Official")).toBeInTheDocument();
    expect(officialRow && within(officialRow).getByText(/github.com\/Jizai-inc\/palmimo-devkit|Jizai-inc\/palmimo-devkit/)).toBeInTheDocument();

    const forkRow = rows.find((row) => row !== officialRow);
    expect(forkRow && within(forkRow).getByRole("link", { name: /alice\.teleop/ })).toBeInTheDocument();
    expect(forkRow && within(forkRow).queryByText("Official")).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no apps", async () => {
    server.use(getListAppsApiV1AppsGetMockHandler({ apps: [] }), getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM));
    renderWithRouter(<AppsListPanel />);

    expect(await screen.findByText("No apps installed yet.")).toBeInTheDocument();
  });

  it("resets an old-format ledger after confirmation", async () => {
    const user = userEvent.setup();
    let reset = false;
    server.use(
      http.get("*/api/v1/apps", () =>
        reset
          ? HttpResponse.json({ apps: [] })
          : HttpResponse.json({ error: { code: "ledger_legacy", params: {} } }, { status: 409 }),
      ),
      http.post("*/api/v1/apps/reset", () => {
        reset = true;
        return HttpResponse.json({ paths: [], leftover_paths: [] }, { status: 202 });
      }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    expect(await screen.findByText("Reset the app ledger")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset apps" }));
    expect(await screen.findByRole("heading", { name: "Reset all apps?" })).toBeInTheDocument();
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Reset apps" }));

    expect(await screen.findByText("No apps installed yet.")).toBeInTheDocument();
  });

  // Without this, an unreadable app ledger would leave the operator unable to recover from the apps screen.
  it("resets an unreadable ledger after confirmation", async () => {
    const user = userEvent.setup();
    let reset = false;
    server.use(
      http.get("*/api/v1/apps", () =>
        reset
          ? HttpResponse.json({ apps: [] })
          : HttpResponse.json({ error: { code: "platform_state_corrupt", params: {} } }, { status: 409 }),
      ),
      http.post("*/api/v1/apps/reset", () => {
        reset = true;
        return HttpResponse.json({ paths: [], leftover_paths: [] }, { status: 202 });
      }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    expect(await screen.findByText("Reset the unreadable app ledger")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reset apps" }));
    expect(await screen.findByRole("heading", { name: "Reset all apps?" })).toBeInTheDocument();
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Reset apps" }));

    expect(await screen.findByText("No apps installed yet.")).toBeInTheDocument();
  });

  it("keeps the reset dialog open and shows leftover paths", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/v1/apps", () => HttpResponse.json({ error: { code: "ledger_legacy", params: {} } }, { status: 409 })),
      http.post("*/api/v1/apps/reset", () => HttpResponse.json({ paths: [], leftover_paths: ["/var/lib/palmimo/apps/alice.app"] }, { status: 202 })),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    await user.click(await screen.findByRole("button", { name: "Reset apps" }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Reset apps" }));

    expect(await screen.findByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText("/var/lib/palmimo/apps/alice.app")).toBeInTheDocument();
  });

  // Without this, a device stuck on an old platform version would let the operator kick off an
  // install/start that the backend will 409 on (platform_not_ready) instead of being steered to
  // the update banner first.
  it("shows the platform-not-ready banner and disables Start until the platform is updated", async () => {
    const user = userEvent.setup();
    let updateStarted = false;
    server.use(
      getListAppsApiV1AppsGetMockHandler({ apps: [app({ status: "stopped" })] }),
      getGetPlatformApiV1PlatformGetMockHandler({
        ...READY_PLATFORM,
        ready: false,
        latest: { version: 2, tag: "v2", summary: "adds camera support", requires_portal: "0.1.0", restart_portal: false, reflash_required: false },
      }),
      http.post("*/api/v1/platform/update", () => {
        updateStarted = true;
        return HttpResponse.json({ job: { state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, target_version: 2 } }, { status: 202 });
      }),
    );
    renderWithRouter(<AppsListPanel />);

    expect(await screen.findByText("Update the device platform")).toBeInTheDocument();
    expect(screen.getByText(/adds camera support/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Update now" }));
    await waitFor(() => expect(updateStarted).toBe(true));
  });

  it("disables Start for every app while another app's install/update/delete is in progress", async () => {
    server.use(
      getListAppsApiV1AppsGetMockHandler({
        apps: [app({ name: "app-a", status: "stopped" }), app({ name: "app-b", status: "updating" })],
      }),
      getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM),
    );
    renderWithRouter(<AppsListPanel />);

    await screen.findByText("app-a");
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });
});
