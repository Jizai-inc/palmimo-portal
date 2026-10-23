import i18n from "@/i18n";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getGetAppApiV1AppsNameGetMockHandler,
  getGetLogsApiV1AppsNameLogsGetMockHandler,
  getListAppsApiV1AppsGetMockHandler,
} from "@/api/generated/apps/apps.msw";
import { getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import type { AppDetailResponse, AppSummary } from "@/api/generated/models";
import { AppDetailPanel } from "@/components/AppDetailPanel";
import { renderWithRouter } from "@/test/render";
import { server } from "@/test/server";

function detail(overrides: Partial<AppDetailResponse> = {}): AppDetailResponse {
  return {
    autostart: false,
    bindings: {},
    broken_reason: null,
    description: "A test app.",
    devices: [],
    env: [
      { name: "WIFI_PASSWORD", required: true, description: "Wi-Fi password", help_url: null },
      { name: "LOG_LEVEL", required: false, description: "Verbosity", help_url: null },
    ],
    installed_at: 1_700_000_000,
    last_job: null,
    manifest: {
      devices: [],
      env: [
        { name: "WIFI_PASSWORD", required: true, description: "Wi-Fi password", help_url: null },
        { name: "LOG_LEVEL", required: false, description: "Verbosity", help_url: null },
      ],
      params: [{ name: "port", type: "int", default: 8765, min: null, max: null, choices: null, pattern: null, flag: null }],
    },
    id: "palmimo.teleop",
    name: "palmimo-teleop",
    params: { port: 8765, verbose: false },
    source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc123" },
    status: "stopped",
    ...overrides,
  };
}

function summary(overrides: Partial<AppSummary> = {}): AppSummary {
  return {
    autostart: false,
    broken_reason: null,
    credential_rejected: false,
    id: "palmimo.teleop",
    installed_at: 1_700_000_000,
    last_job: null,
    latest_commit: null,
    name: "palmimo-teleop",
    source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc123" },
    status: "stopped",
    update_available: false,
    ...overrides,
  };
}

const UPDATE_AVAILABLE = { update_available: true, latest_ref: "v2", latest_commit: "def456" };

describe("AppDetailPanel", () => {
  // LogsSection always fetches on mount regardless of which behavior a test targets; give every
  // test a default empty response so only the logs-specific tests need to know its shape.
  beforeEach(() => {
    server.use(getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  });

  // name (the payload must be {REQ_NAME: STORE_NAME}, never {REQ_NAME: value}), and an unbound
  // required variable would go unnoticed until the app fails to start.
  it("highlights an unbound required variable and saves bindings by secret name only", async () => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [{ name: "HOME_WIFI_PASSWORD", updated_at: 1, used_by: [] }] }),
      http.put("*/api/v1/apps/palmimo.teleop/bindings", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail({ bindings: { WIFI_PASSWORD: "HOME_WIFI_PASSWORD" } }));
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await screen.findByText("WIFI_PASSWORD");
    expect(screen.getByText("This required variable is not bound.")).toBeInTheDocument();
    await within(screen.getByLabelText("WIFI_PASSWORD")).findByRole("option", { name: "HOME_WIFI_PASSWORD" });

    await user.selectOptions(screen.getByLabelText("WIFI_PASSWORD"), "HOME_WIFI_PASSWORD");
    await user.click(screen.getByRole("button", { name: "Save bindings" }));

    await waitFor(() => expect(putBody).toEqual({ bindings: { WIFI_PASSWORD: "HOME_WIFI_PASSWORD" } }));
  });

  it.each([
    ["port", "9000", { port: 9000, verbose: false }],
  ] as const)("saves a numeric parameter as a number, not a string", async (field, typed, expected) => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.put("*/api/v1/apps/palmimo.teleop/params", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail());
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    const input = await screen.findByLabelText(field);
    await user.clear(input);
    await user.type(input, typed);
    await user.click(screen.getByRole("button", { name: "Save parameters" }));

    await waitFor(() => expect(putBody).toEqual({ params: expected }));
  });

  it("renders an enum param as a select with its declared choices", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(
        detail({
          params: { mode: "walk" },
          manifest: {
            devices: [],
            env: [],
            params: [
              { name: "mode", type: "enum", default: "walk", choices: ["walk", "trot"], min: null, max: null, pattern: null, flag: null },
            ],
          },
        }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    const select = await screen.findByLabelText("mode");
    expect(select.tagName).toBe("SELECT");
    expect(within(select).getByRole("option", { name: "walk" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "trot" })).toBeInTheDocument();
  });

  it("shows each param's manifest default until a value is stored", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(
        detail({
          params: {},
          manifest: {
            devices: [],
            env: [],
            params: [
              { name: "port", type: "int", default: 8765, min: null, max: null, choices: null, pattern: null, flag: null },
              { name: "message", type: "string", default: "hello", min: null, max: null, choices: null, pattern: null, flag: null },
              { name: "no_camera", type: "bool", default: true, min: null, max: null, choices: null, pattern: null, flag: "--no-camera" },
            ],
          },
        }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByLabelText("port")).toHaveValue(8765);
    expect(screen.getByLabelText("message")).toHaveValue("hello");
    expect(screen.getByLabelText("no_camera")).toBeChecked();
  });

  it("shows only the selected invocation's logs", async () => {
    const user = userEvent.setup();
    let lastInvocationParam: string | null = null;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.get("*/api/v1/apps/palmimo.teleop/logs", ({ request }) => {
        const url = new URL(request.url);
        // The generated client serializes an absent `invocation` as the literal query string
        // "null" (not an omitted param), so "null" must be treated the same as not-present.
        const raw = url.searchParams.get("invocation");
        lastInvocationParam = raw && raw !== "null" ? raw : null;
        return HttpResponse.json({
          entries: lastInvocationParam === "00000000000000000000000000000000"
            ? [{ message: "old start line", timestamp: 1, invocation_id: "00000000000000000000000000000000" }]
            : [
                { message: "old start line", timestamp: 1, invocation_id: "00000000000000000000000000000000" },
                { message: "current start line", timestamp: 2, invocation_id: "11111111111111111111111111111111" },
              ],
          invocations: [{ id: "11111111111111111111111111111111", started_at: 2 }, { id: "00000000000000000000000000000000", started_at: 1 }],
          next_cursor: null,
        });
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    const startSelect = await screen.findByLabelText("Start");
    await waitFor(() => expect(startSelect).toHaveValue("11111111111111111111111111111111"));
    await user.selectOptions(startSelect, "00000000000000000000000000000000");

    await waitFor(() => expect(lastInvocationParam).toBe("00000000000000000000000000000000"));
    expect(await screen.findByText("old start line")).toBeInTheDocument();
    expect(screen.queryByText("current start line")).not.toBeInTheDocument();
  });

  it.each([
    ["Make default", null, { id: "palmimo.teleop" }],
    ["Remove as default", "palmimo.teleop", { id: null }],
  ])("%s sends PUT /apps/default", async (label, currentDefault, expectedBody) => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [summary()], default_app: currentDefault, autostart_conflict: [] }),
      http.put("*/api/v1/apps/default", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ default_app: (putBody as { id: string | null }).id });
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await user.click(await screen.findByRole("button", { name: label }));

    await waitFor(() => expect(putBody).toEqual(expectedBody));
  });

  it("warns about a required key the server reports missing even when a binding is saved", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(
        detail({
          env: [{ name: "API_KEY", required: true, description: "key", help_url: null }],
          bindings: { API_KEY: "GONE" },
          missing_env: ["API_KEY"],
        }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByText(i18n.t("appDetail.bindingsUnboundRequiredWarning"))).toBeInTheDocument();
  });

  // where the operator expects either log lines or an explanation.
  it("shows a readable message instead of log lines when logs are unavailable", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getGetLogsApiV1AppsNameLogsGetMockHandler({ unavailable: "journal_permission" }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByText("Palmimo cannot read this app's logs right now.")).toBeInTheDocument();
  });

  // a readable label.
  it("shows a human-readable label for a declared device instead of its raw id", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail({ devices: ["motor_display"] })),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByText("Motors & display")).toBeInTheDocument();
    expect(screen.queryByText("motor_display")).not.toBeInTheDocument();
  });

  // explanation of what it does, even though the backend now sends it.
  it("shows a param's description as helper text, but not when it has none", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(
        detail({
          params: { port: 8765, mode: "walk" },
          manifest: {
            devices: [],
            env: [],
            params: [
              { name: "port", type: "int", default: 8765, min: null, max: null, choices: null, pattern: null, flag: null, description: "The port to serve on." },
              { name: "mode", type: "enum", default: "walk", choices: ["walk", "trot"], min: null, max: null, pattern: null, flag: null },
            ],
          },
        }),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await screen.findByLabelText("port");
    expect(screen.getByText("The port to serve on.")).toBeInTheDocument();
  });

  describe("following a newly started invocation", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    // Without following `invocations[0]` on every poll rather than fixing the choice once
    // (auto-selected from the stopped app's last run), starting the app would keep polling the
    // old, stale invocation and never surface the new run's own logs.
    it("shows the new run's logs once a stopped app is started", async () => {
      const user = userEvent.setup();
      let running = false;
      server.use(
        http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json(detail({ status: running ? "running" : "stopped" }))),
        getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
        http.post("*/api/v1/apps/palmimo.teleop/start", () => {
          running = true;
          return HttpResponse.json({ status: "activating" }, { status: 202 });
        }),
        http.get("*/api/v1/apps/palmimo.teleop/logs", ({ request }) => {
          const url = new URL(request.url);
          const raw = url.searchParams.get("invocation");
          const requested = raw && raw !== "null" ? raw : null;
          const invocations = running
            ? [{ id: "22222222222222222222222222222222", started_at: 2 }, { id: "11111111111111111111111111111111", started_at: 1 }]
            : [{ id: "11111111111111111111111111111111", started_at: 1 }];
          const allEntries = [
            { message: "old run line", timestamp: 1, invocation_id: "11111111111111111111111111111111" },
            ...(running ? [{ message: "new run line", timestamp: 2, invocation_id: "22222222222222222222222222222222" }] : []),
          ];
          const entries = requested ? allEntries.filter((entry) => entry.invocation_id === requested) : allEntries;
          return HttpResponse.json({ entries, invocations, next_cursor: null });
        }),
      );
      renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

      await waitFor(() => expect(screen.getByText("old run line")).toBeInTheDocument());

      await user.click(await screen.findByRole("button", { name: "Start" }));
      await act(() => vi.advanceTimersByTimeAsync(2_000));

      await waitFor(() => expect(screen.getByText("new run line")).toBeInTheDocument());
    });
  });

  // AppJobDialog's `onDone` fires the moment the delete job is observed done, but the operator
  // may not click "Close" right away -- any other fetch of the now-deleted app in that window
  // (e.g. a window-focus refetch) must not take the whole panel down with it.
  it("stays answerable after a stray app refetch 404s once the delete has finished", async () => {
    const user = userEvent.setup();
    const onDeleted = vi.fn();
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.delete("*/api/v1/apps/palmimo.teleop", () =>
        HttpResponse.json({ job: { id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 }),
      ),
      http.get("*/api/v1/apps/jobs/job-del", () =>
        HttpResponse.json({ id: "job-del", kind: "delete", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" onDeleted={onDeleted} />);

    await user.click(await screen.findByRole("button", { name: "Delete this app" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete app" }));
    await screen.findByRole("status");

    server.use(
      http.get("*/api/v1/apps/palmimo.teleop", () =>
        HttpResponse.json({ error: { code: "app_not_found", params: {} } }, { status: 404 }),
      ),
    );
    // Regains window focus, the real trigger the report described -- react-query's own
    // `refetchOnWindowFocus` refetch, which (unlike a manual `refetchQueries` call) respects the
    // app query being disabled for the duration of the delete job.
    await act(async () => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });

  // Closing the job dialog does not cancel the job (its own note says so) -- clicking Close
  // before the delete job ever reports "done" must not strand the app-detail query disabled
  // forever, nor crash once the app is actually gone.
  it("returns to the list once the app disappears after Close is clicked mid-delete", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup();
    const onDeleted = vi.fn();
    let appGone = false;
    server.use(
      http.get("*/api/v1/apps/palmimo.teleop", () =>
        appGone
          ? HttpResponse.json({ error: { code: "app_not_found", params: {} } }, { status: 404 })
          : HttpResponse.json(detail()),
      ),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.delete("*/api/v1/apps/palmimo.teleop", () =>
        HttpResponse.json({ job: { id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 }),
      ),
      // Still running when Close is clicked -- the job never reaches "done" from this
      // component's point of view.
      http.get("*/api/v1/apps/jobs/job-del", () =>
        HttpResponse.json({ id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" onDeleted={onDeleted} />);

    await user.click(await screen.findByRole("button", { name: "Delete this app" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete app" }));

    await user.click(await screen.findByRole("button", { name: "Close" }));
    expect(onDeleted).not.toHaveBeenCalled();

    appGone = true;
    await act(() => vi.advanceTimersByTimeAsync(3_000));
    vi.useRealTimers();

    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });

  // AppDetailPanel.tsx:189's title condition used to be "not yet reported done", which left a
  // failed delete still titled "Deleting palmimo-teleop…" even though the body below it was
  // already showing the failure.
  it("stops claiming to still be deleting once the delete job fails", async () => {
    const user = userEvent.setup();
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.delete("*/api/v1/apps/palmimo.teleop", () =>
        HttpResponse.json({ job: { id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 }),
      ),
      http.get("*/api/v1/apps/jobs/job-del", () =>
        HttpResponse.json({ id: "job-del", kind: "delete", state: "failed", step: "register", error: "disk full", started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await user.click(await screen.findByRole("button", { name: "Delete this app" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete app" }));

    await screen.findByText(/disk full/);
    expect(screen.queryByText("Deleting palmimo-teleop…")).not.toBeInTheDocument();
  });

  it("deletes the app after confirming the dialog, then reports completion once the job finishes", async () => {
    const user = userEvent.setup();
    const onDeleted = vi.fn();
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.delete("*/api/v1/apps/palmimo.teleop", () =>
        HttpResponse.json({ job: { id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 }),
      ),
      http.get("*/api/v1/apps/jobs/job-del", () =>
        HttpResponse.json({ id: "job-del", kind: "delete", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" onDeleted={onDeleted} />);

    await user.click(await screen.findByRole("button", { name: "Delete this app" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete app" }));

    await screen.findByRole("status");
    await user.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });

  it("shows the updated source and removes the update action when an update job finishes", async () => {
    const user = userEvent.setup();
    let updated = false;
    server.use(
      http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json(detail({ source: { type: "git", official: false, url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: updated ? "v2" : "v1", commit: updated ? "def456" : "abc123" } }))),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.post("*/api/v1/apps/palmimo.teleop/update/check", () => HttpResponse.json({ update_available: true, latest_ref: "v2", latest_commit: "def456" })),
      http.post("*/api/v1/apps/palmimo.teleop/update", () => HttpResponse.json({ job: { id: "job-update", kind: "update", state: "running", step: "sync", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 })),
      http.get("*/api/v1/apps/jobs/job-update", () => {
        updated = true;
        return HttpResponse.json({ id: "job-update", kind: "update", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false });
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await user.click(await screen.findByRole("button", { name: "Check for updates" }));
    await user.click(await screen.findByRole("button", { name: "Update" }));

    await waitFor(() => expect(screen.getByText("v2")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Update" })).not.toBeInTheDocument();
  });

  it("disables Update once the app is started here, though the apps list still shows it stopped", async () => {
    const user = userEvent.setup();
    let started = false;
    server.use(
      http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json(detail({ status: started ? "starting" : "stopped" }))),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [summary({ status: "stopped" })] }),
      http.post("*/api/v1/apps/palmimo.teleop/update/check", () => HttpResponse.json(UPDATE_AVAILABLE)),
      http.post("*/api/v1/apps/palmimo.teleop/start", () => {
        started = true;
        return HttpResponse.json({ status: "activating" }, { status: 202 });
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
    await user.click(await screen.findByRole("button", { name: "Check for updates" }));
    expect(await screen.findByRole("button", { name: "Update" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Update" })).toBeDisabled());
    expect(screen.getByText("Stop the running app before updating an app.")).toBeInTheDocument();
  });

  it("keeps Update disabled while another app is running", async () => {
    const user = userEvent.setup();
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({
        apps: [summary(), summary({ id: "palmimo.companion", name: "palmimo-companion", status: "running" })],
      }),
      http.post("*/api/v1/apps/palmimo.teleop/update/check", () => HttpResponse.json(UPDATE_AVAILABLE)),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
    await user.click(await screen.findByRole("button", { name: "Check for updates" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Update" })).toBeDisabled());
  });

  describe("an app that exits on its own", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it("shows a running app as stopped once it exits on its own", async () => {
      let exited = false;
      server.use(
        http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json(detail({ status: exited ? "stopped" : "running" }))),
        getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
        getListAppsApiV1AppsGetMockHandler({ apps: [summary({ status: "running" })] }),
      );
      renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
      await screen.findByText("Running");

      exited = true;
      await act(() => vi.advanceTimersByTimeAsync(3_000));

      await waitFor(() => expect(screen.getByText("Stopped")).toBeInTheDocument());
      expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    });

    it("enables Update once the app has exited, though the apps list still shows it running", async () => {
      const user = userEvent.setup();
      let exited = false;
      server.use(
        http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json(detail({ status: exited ? "stopped" : "running" }))),
        getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
        getListAppsApiV1AppsGetMockHandler({ apps: [summary({ status: "running" })] }),
        http.post("*/api/v1/apps/palmimo.teleop/update/check", () => HttpResponse.json(UPDATE_AVAILABLE)),
      );
      renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
      await user.click(await screen.findByRole("button", { name: "Check for updates" }));
      expect(await screen.findByRole("button", { name: "Update" })).toBeDisabled();

      exited = true;
      await act(() => vi.advanceTimersByTimeAsync(3_000));

      await waitFor(() => expect(screen.getByRole("button", { name: "Update" })).toBeEnabled());
    });
  });
});


it("switches app descriptions without losing unsaved parameter values", async () => {
  const user = userEvent.setup();
  const app = detail({
    translations: { ja: { description: "Localized summary", "env.WIFI_PASSWORD": "Localized key", "params.port": "Localized port" } },
    manifest: { devices: [], env: [], params: [{ name: "port", type: "int", default: 8765, description: "Port description" }] },
  });
  server.use(
    getGetAppApiV1AppsNameGetMockHandler(app),
    getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }),
    getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
  );
  renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
  const input = await screen.findByLabelText("port");
  await user.clear(input);
  await user.type(input, "9000");
  try {
    await act(() => i18n.changeLanguage("ja"));
    expect(screen.getByText("Localized summary")).toBeInTheDocument();
    expect(screen.getByText("Localized key")).toBeInTheDocument();
    expect(screen.getByText("Localized port")).toBeInTheDocument();
    expect(input).toHaveValue(9000);
    await act(() => i18n.changeLanguage("en"));
    expect(screen.getByText("A test app.")).toBeInTheDocument();
    expect(screen.getByText("Port description")).toBeInTheDocument();
    expect(input).toHaveValue(9000);
  } finally {
    await act(() => i18n.changeLanguage("en"));
  }
});
