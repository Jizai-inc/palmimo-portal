import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { PlatformStatusResponse } from "@/api/generated/models";
import { PlatformUpdateCard } from "@/components/PlatformUpdateCard";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const BASE_PLATFORM: PlatformStatusResponse = {
  installed_at: "2026-01-01T00:00:00Z",
  installed_version: 1,
  latest: null,
  latest_error: null,
  ready: true,
  reason: null,
  required_version: 1,
  verify_diffs: [],
};

describe("PlatformUpdateCard", () => {
  it("shows up to date when there is no newer platform version", async () => {
    server.use(http.get("*/api/v1/platform", () => HttpResponse.json(BASE_PLATFORM)));
    renderWithProviders(<PlatformUpdateCard installedPortalVersion="0.1.0" />);

    expect(await screen.findByText("Up to date")).toBeInTheDocument();
  });

  // Without this, starting a platform update whose Portal requirement this device does not meet
  // would let the operator kick off a bundle apply that assumes newer Portal behavior it lacks.
  it("shows the portal-too-old note instead of an update button when Portal is behind requires_portal", async () => {
    server.use(
      http.get("*/api/v1/platform", () =>
        HttpResponse.json({
          ...BASE_PLATFORM,
          latest: { version: 2, tag: "v2", summary: "adds camera support", requires_portal: "0.2.0", restart_portal: false, reflash_required: false },
        }),
      ),
    );
    renderWithProviders(<PlatformUpdateCard installedPortalVersion="0.1.0" />);

    expect(await screen.findByText(/Update the Portal itself/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update" })).not.toBeInTheDocument();
  });

  it("starts a platform update and shows progress", async () => {
    const user = userEvent.setup();
    let started = false;
    server.use(
      http.get("*/api/v1/platform", () =>
        HttpResponse.json({
          ...BASE_PLATFORM,
          latest: { version: 2, tag: "v2", summary: "adds camera support", requires_portal: "0.1.0", restart_portal: false, reflash_required: false },
        }),
      ),
      http.post("*/api/v1/platform/update", () => {
        started = true;
        return HttpResponse.json({ job: { state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, target_version: 2 } }, { status: 202 });
      }),
      http.get("*/api/v1/platform/update", () =>
        HttpResponse.json({ job: { state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, target_version: 2 } }),
      ),
    );
    renderWithProviders(<PlatformUpdateCard installedPortalVersion="0.1.0" />);

    await user.click(await screen.findByRole("button", { name: "Update" }));

    await waitFor(() => expect(started).toBe(true));
  });

  it("checks now and shows the freshly returned latest version", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/v1/platform", () =>
        HttpResponse.json({
          ...BASE_PLATFORM,
          latest: { version: 2, tag: "v2", summary: "adds camera support", requires_portal: "0.1.0", restart_portal: false, reflash_required: false },
        }),
      ),
      http.post("*/api/v1/platform/check", () =>
        HttpResponse.json({
          ...BASE_PLATFORM,
          latest: { version: 3, tag: "v3", summary: "adds a device class", requires_portal: "0.1.0", restart_portal: false, reflash_required: false },
        }),
      ),
    );
    renderWithProviders(<PlatformUpdateCard installedPortalVersion="0.1.0" />);
    await screen.findByText("2 (v2)");

    await user.click(await screen.findByRole("button", { name: "Check now" }));

    expect(await screen.findByText("3 (v3)")).toBeInTheDocument();
  });
});
