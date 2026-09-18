import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { getGetCatalogApiV1CatalogGetMockHandler } from "@/api/generated/catalog/catalog.msw";
import { getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import type { CatalogResponse } from "@/api/generated/models";
import { AddAppPanel } from "@/components/AddAppPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const CATALOG: CatalogResponse = {
  tag: "v1.0.0",
  fetched_at: 1_700_000_000,
  stale: false,
  reason: null,
  apps: [
    {
      name: "palmimo-teleop",
      description: "Drive the robot from a browser.",
      devices: ["camera"],
      env: [
        { name: "WIFI_PASSWORD", required: true, description: "Wi-Fi password for the base station", help_url: null },
        { name: "LOG_LEVEL", required: false, description: "Verbosity", help_url: null },
      ],
      source: {
        type: "git",
        url: "https://github.com/Jizai-inc/palmimo-devkit",
        subdir: "examples/teleop",
        manifest: "palmimo.realtime.toml",
        ref_kind: "tag",
        ref: "v1.0.0",
        commit: null,
      },
    },
  ],
};

describe("AddAppPanel", () => {
  it("shows a catalog card's required and optional env badges", async () => {
    server.use(getGetCatalogApiV1CatalogGetMockHandler(CATALOG));
    renderWithProviders(<AddAppPanel />);

    await screen.findByText("palmimo-teleop");
    expect(screen.getByText("WIFI_PASSWORD")).toBeInTheDocument();
    expect(screen.getByText("Required")).toBeInTheDocument();
    expect(screen.getByText("LOG_LEVEL")).toBeInTheDocument();
    expect(screen.getByText("Optional")).toBeInTheDocument();
  });

  // Without this, an operator would see the raw manifest device id ("camera") instead of a
  // readable label.
  it("shows a human-readable label for a catalog app's declared device instead of its raw id", async () => {
    server.use(getGetCatalogApiV1CatalogGetMockHandler(CATALOG));
    renderWithProviders(<AddAppPanel />);

    expect(await screen.findByText("Camera")).toBeInTheDocument();
    expect(screen.queryByText("camera")).not.toBeInTheDocument();
  });

  // Without this, installing from the catalog card would silently fail to reach the backend
  // with a correctly-shaped git source (name/ref/ref_kind/subdir/manifest), or the job dialog +
  // the caller's completion callback would never fire once the job finishes.
  //
  // `onInstalled` receiving the finished job's `app_name` is what routes/apps.add.tsx uses to
  // navigate straight to the new app's detail page instead of the plain list -- asserted here,
  // at the router-free component boundary, rather than by mocking the router itself.
  it("installs a catalog app and reports completion with the installed app's name", async () => {
    const user = userEvent.setup();
    const onInstalled = vi.fn();
    let installedBody: unknown;
    server.use(
      getGetCatalogApiV1CatalogGetMockHandler(CATALOG),
      http.post("*/api/v1/apps/install", async ({ request }) => {
        installedBody = await request.json();
        return HttpResponse.json({ job: { id: "job-1", app_name: "palmimo-teleop", kind: "install", state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 });
      }),
      http.get("*/api/v1/apps/jobs/job-1", () =>
        HttpResponse.json({ id: "job-1", app_name: "palmimo-teleop", kind: "install", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithProviders(<AddAppPanel onInstalled={onInstalled} />);

    await user.click(await screen.findByRole("button", { name: "Install" }));

    await waitFor(() =>
      expect(installedBody).toEqual({
        source: {
          type: "git",
          url: "https://github.com/Jizai-inc/palmimo-devkit",
          ref: "v1.0.0",
          ref_kind: "tag",
          subdir: "examples/teleop",
          manifest: "palmimo.realtime.toml",
        },
      }),
    );
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith("palmimo-teleop"));
  });

  it("shows the GitHub preview card once a preview succeeds, then allows install", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "my-app", description: "A private app.", devices: [], env: [{ name: "TOKEN", required: true, description: "API token", help_url: null }] }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/org/private-app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("my-app")).toBeInTheDocument();
    expect(screen.getByText("TOKEN")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install" })).toBeInTheDocument();
  });

  it("sends the chosen manifest file with a zip preview and install", async () => {
    const user = userEvent.setup();
    const manifests: (FormDataEntryValue | null)[] = [];
    server.use(
      http.post("*/api/v1/apps/preview", async ({ request }) => {
        manifests.push((await request.formData()).get("manifest"));
        return HttpResponse.json({ name: "my-realtime", description: "d", devices: [], env: [] });
      }),
      http.post("*/api/v1/apps/install", async ({ request }) => {
        manifests.push((await request.formData()).get("manifest"));
        return HttpResponse.json({ job: { id: "job-1", app_name: null, kind: "install", state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 });
      }),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "Zip" }));
    await user.upload(screen.getByLabelText("Choose file"), new File(["z"], "app.zip", { type: "application/zip" }));
    await user.type(screen.getByLabelText("Manifest file (optional)"), "palmimo.realtime.toml");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await user.click(await screen.findByRole("button", { name: "Install" }));

    await waitFor(() => expect(manifests).toEqual(["palmimo.realtime.toml", "palmimo.realtime.toml"]));
  });

  // Without this, a manifest whose help_url predates the backend's scheme validation (or a
  // future backend regression) could get a `javascript:` URL turned into a clickable link,
  // letting an installed app's manifest execute script in the operator's browser session.
  it("only renders an env help_url as a link when its scheme is http(s)", async () => {
    server.use(
      getGetCatalogApiV1CatalogGetMockHandler({
        ...CATALOG,
        apps: [
          {
            ...CATALOG.apps[0],
            env: [
              { name: "WIFI_PASSWORD", required: true, description: "Wi-Fi password", help_url: "javascript:alert(1)" },
              { name: "LOG_LEVEL", required: false, description: "Verbosity", help_url: "https://example.com/docs" },
            ],
          },
        ],
      }),
    );
    renderWithProviders(<AddAppPanel />);

    await screen.findByText("palmimo-teleop");
    expect(screen.getAllByRole("link", { name: "Learn more" })).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "Learn more" })?.getAttribute("href")).toBe("https://example.com/docs");
  });

  // Without this, an "offline" catalog fetch failure would show the same generic stale message
  // as a clock-sync or rate-limit failure, leaving the operator without a next step.
  it("shows the offline-specific stale-catalog message distinct from the generic one", async () => {
    server.use(getGetCatalogApiV1CatalogGetMockHandler({ ...CATALOG, stale: true, reason: "offline" }));
    renderWithProviders(<AddAppPanel />);

    expect(await screen.findByText(/this device appears to be offline/)).toBeInTheDocument();
    expect(screen.queryByText("Showing the last known catalog — could not refresh it just now.")).not.toBeInTheDocument();
  });

  // Without this, an operator previewing a private app could install it without noticing a
  // required secret was never registered, and only find out once the app fails to start.
  it("marks a required preview env var with no matching secret as not yet registered", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({
          name: "my-app",
          description: "A private app.",
          devices: [],
          env: [
            { name: "TOKEN", required: true, description: "API token", help_url: null },
            { name: "WIFI_PASSWORD", required: true, description: "Wi-Fi password", help_url: null },
          ],
        }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [{ name: "WIFI_PASSWORD", updated_at: 1, used_by: [] }] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/org/private-app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    await screen.findByText("TOKEN");
    const tokenRow = screen.getByText("TOKEN").closest("li");
    const wifiRow = screen.getByText("WIFI_PASSWORD").closest("li");
    expect(tokenRow && within(tokenRow).getByText("Not yet registered")).toBeInTheDocument();
    expect(wifiRow && within(wifiRow).queryByText("Not yet registered")).not.toBeInTheDocument();
  });
});
