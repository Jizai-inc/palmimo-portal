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
  return {
    autostart: false,
    broken_reason: null,
    credential_rejected: false,
    installed_at: 1_700_000_000,
    last_job: null,
    latest_commit: null,
    name: "palmimo-teleop",
    source: { type: "git", url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc" },
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

  it("shows an empty state when there are no apps", async () => {
    server.use(getListAppsApiV1AppsGetMockHandler({ apps: [] }), getGetPlatformApiV1PlatformGetMockHandler(READY_PLATFORM));
    renderWithRouter(<AppsListPanel />);

    expect(await screen.findByText("No apps installed yet.")).toBeInTheDocument();
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
});
