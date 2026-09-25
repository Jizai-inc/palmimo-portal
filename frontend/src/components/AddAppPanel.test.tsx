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
        official: true,
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

const JOB_RESPONSE = {
  job: { id: "job-1", app_id: "palmimo.teleop", kind: "install", state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false },
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

  // Without this, previewing then installing a catalog app would silently fail to reach the
  // backend with a correctly-shaped git source and the chosen device-name part, or the job
  // dialog + the caller's completion callback would never fire once the job finishes.
  //
  // `onInstalled` receiving the finished job's `app_id` is what routes/apps.add.tsx uses to
  // navigate straight to the new app's detail page instead of the plain list -- asserted here,
  // at the router-free component boundary, rather than by mocking the router itself.
  it("previews then installs a catalog app, sending the default device name and reporting completion with the installed app's id", async () => {
    const user = userEvent.setup();
    const onInstalled = vi.fn();
    let installedBody: unknown;
    let jobPolls = 0;
    server.use(
      getGetCatalogApiV1CatalogGetMockHandler(CATALOG),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "palmimo-teleop", namespace: "palmimo", suggested_name: "teleop", suggested_id: "palmimo.teleop", description: "Drive the robot from a browser.", devices: ["camera"], env: [] }),
      ),
      http.post("*/api/v1/apps/install", async ({ request }) => {
        installedBody = await request.json();
        return HttpResponse.json(JOB_RESPONSE, { status: 202 });
      }),
      http.get("*/api/v1/apps/jobs/job-1", () => {
        jobPolls += 1;
        return HttpResponse.json({ id: "job-1", app_id: "palmimo.teleop", kind: "install", state: jobPolls === 1 ? "running" : "done", step: "register", error: null, started_at: 1, finished_at: jobPolls === 1 ? null : 2, dropped_bindings: [], dropped_params: [], lock_generated: false });
      }),
    );
    renderWithProviders(<AddAppPanel onInstalled={onInstalled} />);

    await screen.findByText("palmimo-teleop");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByLabelText("Name on this device")).toHaveValue("teleop");
    await user.click(screen.getByRole("button", { name: "Install" }));

    expect(await screen.findByRole("heading", { name: /palmimo-teleop/ })).toBeInTheDocument();

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
        name: "teleop",
      }),
    );
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith("palmimo.teleop"));
  });

  // Without this, the namespace segment could be typed over (letting an operator claim to be
  // "palmimo." on a fork) or a device-name-part change could fail to reach the install request
  // (design doc 3.9's add-app screen: namespace read-only, name part editable and sent).
  it("shows the namespace as read-only and sends an edited device name part on install", async () => {
    const user = userEvent.setup();
    let installedName: string | undefined;
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "my-app", namespace: "alice", suggested_name: "app", suggested_id: "alice.app", description: "A private app.", devices: [], env: [] }),
      ),
      http.post("*/api/v1/apps/install", async ({ request }) => {
        installedName = ((await request.json()) as { name?: string }).name;
        return HttpResponse.json(JOB_RESPONSE, { status: 202 });
      }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/alice/app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));

    expect(await screen.findByText("alice.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "alice." })).not.toBeInTheDocument();

    const nameInput = screen.getByLabelText("Name on this device");
    await user.clear(nameInput);
    await user.type(nameInput, "my-fork");
    await user.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() => expect(installedName).toBe("my-fork"));
  });

  // Without this, a name-part collision discovered only at install time (design doc 3.9: preview
  // and install can race) would leave the operator stuck retrying an id that will never install,
  // instead of being shown a fresh suggested name.
  it("re-runs preview and shows a new suggested name when install returns app_exists", async () => {
    const user = userEvent.setup();
    let previewCount = 0;
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.post("*/api/v1/apps/preview", () => {
        previewCount += 1;
        const suggestedName = previewCount === 1 ? "app" : "app-2";
        return HttpResponse.json({ name: "my-app", namespace: "alice", suggested_name: suggestedName, suggested_id: `alice.${suggestedName}`, description: "d", devices: [], env: [] });
      }),
      http.post("*/api/v1/apps/install", () =>
        HttpResponse.json({ error: { code: "app_exists", params: {} } }, { status: 409 }),
      ),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/alice/app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByLabelText("Name on this device")).toHaveValue("app"));

    await user.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() => expect(screen.getByLabelText("Name on this device")).toHaveValue("app-2"));
    expect(previewCount).toBe(2);
  });

  it("shows the GitHub preview card once a preview succeeds, then allows install", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "my-app", namespace: "alice", suggested_name: "private-app", suggested_id: "alice.private-app", description: "A private app.", devices: [], env: [{ name: "TOKEN", required: true, description: "API token", help_url: null }] }),
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

  it("clears a GitHub preview before installation when its source changes", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "alice-app", namespace: "alice", suggested_name: "alice-app", suggested_id: "alice.alice-app", description: "d", devices: [], env: [] }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    const url = screen.getByLabelText("Repository URL");
    await user.type(url, "https://github.com/alice/app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByRole("button", { name: "Install" });

    await user.clear(url);
    await user.type(url, "https://github.com/bob/other");

    expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
  });

  it("does not restore a pending GitHub preview after its source changes", async () => {
    const user = userEvent.setup();
    let respond: ((response: Response) => void) | undefined;
    server.use(
      http.post(
        "*/api/v1/apps/preview",
        () =>
          new Promise((resolve) => {
            respond = resolve;
          }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    const url = screen.getByLabelText("Repository URL");
    await user.type(url, "https://github.com/alice/app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(respond).toBeDefined());

    await user.clear(url);
    await user.type(url, "https://github.com/bob/other");
    respond?.(HttpResponse.json({ name: "alice-app", namespace: "alice", suggested_name: "alice-app", suggested_id: "alice.alice-app", description: "d", devices: [], env: [] }));

    await waitFor(() => expect(screen.queryByText("alice-app")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Install" })).not.toBeInTheDocument();
  });

  it("sends the chosen manifest file with a zip preview and install", async () => {
    const user = userEvent.setup();
    const manifests: (FormDataEntryValue | null)[] = [];
    server.use(
      http.post("*/api/v1/apps/preview", async ({ request }) => {
        manifests.push((await request.formData()).get("manifest"));
        return HttpResponse.json({ name: "my-realtime", namespace: "zip", suggested_name: "my-realtime", suggested_id: "zip.my-realtime", description: "d", devices: [], env: [] });
      }),
      http.post("*/api/v1/apps/install", async ({ request }) => {
        manifests.push((await request.formData()).get("manifest"));
        return HttpResponse.json({ job: { id: "job-1", app_id: null, kind: "install", state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 });
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
          namespace: "alice",
          suggested_name: "my-app",
          suggested_id: "alice.my-app",
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

  // Without this, typing a device name part outside the backend's `^[a-z][a-z0-9-]{0,39}$`
  // pattern would only be caught after a round trip to /install, instead of being flagged
  // (and blocking Install) right in the form.
  it("disables Install and shows a hint when the device name part is invalid", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/apps/preview", () =>
        HttpResponse.json({ name: "my-app", namespace: "alice", suggested_name: "app", suggested_id: "alice.app", description: "d", devices: [], env: [] }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithProviders(<AddAppPanel />);

    await user.click(screen.getByRole("button", { name: "GitHub URL" }));
    await user.type(screen.getByLabelText("Repository URL"), "https://github.com/alice/app");
    await user.type(screen.getByLabelText("Branch or tag"), "main");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const nameInput = await screen.findByLabelText("Name on this device");

    await user.clear(nameInput);
    await user.type(nameInput, "Not Valid!");

    expect(screen.getByRole("button", { name: "Install" })).toBeDisabled();
    expect(screen.getByText(/lowercase letter/)).toBeInTheDocument();
  });
});
