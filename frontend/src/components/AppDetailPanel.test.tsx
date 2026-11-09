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
import { chooseOption, selectedOptionIndex } from "@/test/select";
import { server } from "@/test/server";

function detail(overrides: Partial<AppDetailResponse> = {}): AppDetailResponse {
  const result: AppDetailResponse = {
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
  if (result.source.public_app_id && result.latest_commit && !result.latest_source) {
    result.latest_source = { ...result.source, commit: result.latest_commit, devices: result.devices, env: result.env };
  }
  return result;
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
    await waitFor(() => expect(screen.getByRole("combobox", { name: "WIFI_PASSWORD" })).toHaveTextContent("Not bound"));
    await chooseOption(user, "WIFI_PASSWORD", "HOME_WIFI_PASSWORD");
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

  it("renames the app and shows the new label with the id beneath it", async () => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.put("*/api/v1/apps/palmimo.teleop/label", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail({ label: "My teleop" }));
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await user.click(await screen.findByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "App name" });
    expect(input).toHaveValue("teleop");
    await user.clear(input);
    await user.type(input, "My teleop{Enter}");

    await waitFor(() => expect(putBody).toEqual({ label: "My teleop" }));
    expect(await screen.findByRole("heading", { name: "My teleop" })).toBeInTheDocument();
    expect(screen.getByText("palmimo.", { exact: false })).toBeInTheDocument();
  });

  it("renders an enum param as a dropdown with its declared choices", async () => {
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

    const trigger = await screen.findByRole("combobox", { name: "mode" });
    expect(trigger).toHaveTextContent("walk");
    await userEvent.setup().click(trigger);
    expect(await screen.findByRole("option", { name: "walk" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "trot" })).toBeInTheDocument();
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

    await screen.findByRole("combobox", { name: "Start" });
    expect(await selectedOptionIndex(user, "Start")).toBe(0);
    await chooseOption(user, "Start", 1);

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

  it("disables Make default for a broken app", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail({ broken_reason: "venv_missing" })),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [summary()], default_app: null, autostart_conflict: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Make default" })).toBeDisabled());
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

  it("shows a readable message instead of log lines when logs are unavailable", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getGetLogsApiV1AppsNameLogsGetMockHandler({ unavailable: "journal_permission" }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByText("Palmimo cannot read this app's logs right now.")).toBeInTheDocument();
  });

  it("shows a human-readable label for a declared device instead of its raw id", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail({ devices: ["motor_display"] })),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    expect(await screen.findByText("Motors & display")).toBeInTheDocument();
    expect(screen.queryByText("motor_display")).not.toBeInTheDocument();
  });

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

it.each([true, false])("uses API update state for a public app with availability %s and links its page", async (available) => {
  const publicId = "019a3f2c-0000-7000-8000-000000000001";
  const record = detail({ update_available: true, latest_commit: "b".repeat(40), public_app_available: available, source: { ...detail().source, public_app_id: publicId, public_page_url: `https://apps.palmimo.dev/apps/${publicId}` } });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  renderWithRouter(<AppDetailPanel id={record.id} />);
  expect(await screen.findByRole("link", { name: i18n.t("appAdd.publicPageLink") })).toHaveAttribute("href", `https://apps.palmimo.dev/apps/${publicId}`);
  if (available) expect(screen.getByRole("button", { name: i18n.t("appDetail.updateButton") })).toBeInTheDocument();
  else {
    expect(screen.getByText(i18n.t("apps.publicAppUnavailable"))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: i18n.t("appDetail.updateButton") })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: i18n.t("appDetail.checkForUpdatesButton") })).not.toBeInTheDocument();
  }
});

it("refreshes installed apps and public catalog after updating", async () => {
  let updated = false;
  let listGets = 0;
  let catalogGets = 0;
  const record = detail({ update_available: true, latest_commit: "b".repeat(40), public_app_available: true, source: { ...detail().source, public_app_id: "019a3f2c-0000-7000-8000-000000000001" } });
  const done = { id: "update-public", app_id: record.id, kind: "update", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, lock_generated: false, dropped_bindings: [], dropped_params: [] };
  server.use(
    http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json({ ...record, update_available: !updated })),
    http.get("*/api/v1/apps", () => { listGets++; return HttpResponse.json({ apps: [summary({ update_available: !updated })], default_app: null, autostart_conflict: [] }); }),
    http.get("*/api/v1/catalog/public", () => { catalogGets++; return HttpResponse.json({ generated_at: "2026-11-09T00:00:00Z", apps: [], fetched_at: 1, stale: false, reason: null }); }),
    getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }),
    http.post("*/api/v1/apps/palmimo.teleop/update", () => { updated = true; return HttpResponse.json({ job: done }, { status: 202 }); }),
    http.get("*/api/v1/apps/jobs/update-public", () => HttpResponse.json(done)),
  );
  const user = userEvent.setup();
  const { queryClient } = renderWithRouter(<AppDetailPanel id={record.id} />);
  await queryClient.fetchQuery({ queryKey: ["/api/v1/catalog/public"], queryFn: () => fetch("/api/v1/catalog/public").then((response) => response.json()) });
  await user.click(await screen.findByRole("button", { name: i18n.t("appDetail.updateButton") }));
  await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: i18n.t("appDetail.updateButton") }));
  await waitFor(() => { expect(listGets).toBeGreaterThan(1); expect(catalogGets).toBeGreaterThan(1); });
  await waitFor(() => expect(screen.queryByRole("button", { name: i18n.t("appDetail.updateButton") })).not.toBeInTheDocument());
});

it("hides previous app detail after a failed refetch", async () => {
  server.use(getGetAppApiV1AppsNameGetMockHandler(detail()), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  const { queryClient } = renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
  await screen.findByText(detail().description);
  server.use(http.get("*/api/v1/apps/palmimo.teleop", () => HttpResponse.json({ error: { code: "public_catalog_unavailable", params: {} } }, { status: 503 })));
  await act(() => queryClient.refetchQueries());
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.queryByText(detail().description)).not.toBeInTheDocument();
});

it("does not show a ledger update flag for a non-public app before checking", async () => {
  server.use(getGetAppApiV1AppsNameGetMockHandler(detail({ update_available: true })), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);
  await screen.findByRole("button", { name: i18n.t("appDetail.checkForUpdatesButton") });
  expect(screen.queryByRole("button", { name: i18n.t("appDetail.updateButton") })).not.toBeInTheDocument();
});

it.each(["tag", "branch"])("does not allow ref edits for an official public %s app", async (kind) => {
  const record = detail({ source: { ...detail().source, public_app_id: "public-id", official: true, ref_kind: kind } });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  renderWithRouter(<AppDetailPanel id={record.id} />);
  await screen.findByText(record.description);
  expect(screen.queryByRole("button", { name: i18n.t("appDetail.sourceEditRefButton") })).not.toBeInTheDocument();
});

it("confirms the displayed public update commit and changed source", async () => {
  const source = { ...detail().source, public_app_id: "public-id" };
  const target = { ...source, url: "https://github.com/new-owner/new-app", commit: "b".repeat(40), devices: [], env: [] };
  const posted = vi.fn();
  const record = detail({ source, latest_source: target, latest_commit: target.commit, update_available: true, public_app_available: true });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }), http.post("*/api/v1/apps/palmimo.teleop/update", async ({ request }) => { posted(await request.json()); return HttpResponse.json({ error: { code: "public_app_changed", params: {} } }, { status: 409 }); }));
  const user = userEvent.setup();
  renderWithRouter(<AppDetailPanel id={record.id} />);
  await user.click(await screen.findByRole("button", { name: i18n.t("appDetail.updateButton") }));
  const dialog = within(screen.getByRole("alertdialog"));
  expect(dialog.getByRole("link", { name: target.url })).toHaveAttribute("href", target.url);
  expect(posted).not.toHaveBeenCalled();
  await user.click(dialog.getByRole("button", { name: i18n.t("appDetail.updateButton") }));
  await waitFor(() => expect(posted).toHaveBeenCalledWith({ commit: target.commit }));
});

it("preserves the last known official badge while public catalog state is unknown", async () => {
  const known = detail({ update_available: false, public_app_available: true, source: { ...detail().source, public_app_id: "public-id", official: true } });
  server.use(getGetAppApiV1AppsNameGetMockHandler(known), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  const { queryClient } = renderWithRouter(<AppDetailPanel id={known.id} />);
  await screen.findByText(i18n.t("appAdd.catalogOfficialBadge"));
  server.use(getGetAppApiV1AppsNameGetMockHandler({ ...known, update_available: null, public_app_available: null, source: { ...known.source, official: null } }));
  await act(() => queryClient.refetchQueries());
  expect(screen.getByText(i18n.t("appAdd.catalogOfficialBadge"))).toBeInTheDocument();
  expect(screen.queryByText(i18n.t("apps.publicAppUnavailable"))).not.toBeInTheDocument();
});

it("warns that saving a community ref opts out of catalog updates", async () => {
  const record = detail({ source: { ...detail().source, public_app_id: "public-id" } });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  const user = userEvent.setup();
  renderWithRouter(<AppDetailPanel id={record.id} />);
  await user.click(await screen.findByRole("button", { name: i18n.t("appDetail.sourceEditRefButton") }));
  expect(screen.getByRole("note")).toHaveTextContent(i18n.t("appDetail.publicRefOptOut"));
});

it.each([
  ["broken", false],
  ["needs_repair", false],
  ["broken", true],
  ["needs_repair", true],
] as const)("confirms public repair from %s with a changed version %s", async (status, changesVersion) => {
  const source = { ...detail().source, public_app_id: "public-id" };
  const commit = changesVersion ? "b".repeat(40) : source.commit;
  const latest = { ...source, commit, devices: changesVersion ? ["camera"] : [], env: changesVersion ? [...detail().env, { name: "REPAIR_TOKEN", required: true, description: "Token", help_url: null }] : detail().env };
  const record = detail({ source, status, broken_reason: "venv_missing", update_available: changesVersion, public_app_available: true, latest_commit: commit, latest_source: latest });
  const posted = vi.fn();
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }), http.post("*/api/v1/apps/palmimo.teleop/update", async ({ request }) => { posted(await request.json()); return HttpResponse.json({ error: { code: "public_app_changed", params: {} } }, { status: 409 }); }));
  const user = userEvent.setup();
  renderWithRouter(<AppDetailPanel id={record.id} />);
  const action = i18n.t(changesVersion ? "appDetail.updateAndRepair" : "apps.repairButton");
  await user.click(await screen.findByRole("button", { name: action }));
  const dialog = within(screen.getByRole("alertdialog"));
  expect(dialog.getByRole("heading", { name: i18n.t(changesVersion ? "appDetail.updateAndRepair" : "appDetail.confirmPublicRepair") })).toBeInTheDocument();
  if (changesVersion) {
    expect(dialog.getByRole("note")).toHaveTextContent(i18n.t("appDetail.repairVersionChange", { current: source.commit, target: commit }));
    expect(within(dialog.getByRole("list", { name: i18n.t("appDetail.addedDevices") })).getByText(i18n.t("devices.camera"))).toBeInTheDocument();
    expect(within(dialog.getByRole("list", { name: i18n.t("appDetail.addedRequiredEnv") })).getByText("REPAIR_TOKEN")).toBeInTheDocument();
  } else {
    expect(dialog.queryByRole("note")).not.toBeInTheDocument();
  }
  expect(posted).not.toHaveBeenCalled();
  await user.click(dialog.getByRole("button", { name: action }));
  await waitFor(() => expect(posted).toHaveBeenCalledWith({ commit }));
});

it("shows added device and required environment requirements before confirming", async () => {
  const source = { ...detail().source, public_app_id: "public-id" };
  const latest = { ...source, commit: "b".repeat(40), devices: ["audio", "camera"], env: [{ name: "NEW_TOKEN", required: true, description: "Token", help_url: null }, { name: "OPTIONAL", required: false, description: "Optional", help_url: null }] };
  const record = detail({ source, devices: ["audio"], env: [], update_available: true, latest_commit: latest.commit, public_app_available: true, latest_source: latest });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  const user = userEvent.setup();
  renderWithRouter(<AppDetailPanel id={record.id} />);
  await user.click(await screen.findByRole("button", { name: i18n.t("appDetail.updateButton") }));
  const dialog = within(screen.getByRole("alertdialog"));
  const devices = within(dialog.getByRole("list", { name: i18n.t("appDetail.addedDevices") }));
  expect(devices.getByText(i18n.t("devices.camera"))).toBeInTheDocument();
  expect(devices.queryByText(i18n.t("devices.audio"))).not.toBeInTheDocument();
  const env = within(dialog.getByRole("list", { name: i18n.t("appDetail.addedRequiredEnv") }));
  expect(env.getByText("NEW_TOKEN")).toBeInTheDocument();
  expect(env.queryByText("OPTIONAL")).not.toBeInTheDocument();
});

it("explains that unknown public catalog state prevents ref editing", async () => {
  const record = detail({ source: { ...detail().source, public_app_id: "public-id", official: null }, update_available: null, public_app_available: null });
  server.use(getGetAppApiV1AppsNameGetMockHandler(record), getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }), getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  renderWithRouter(<AppDetailPanel id={record.id} />);
  expect(await screen.findByRole("note")).toHaveTextContent(i18n.t("appDetail.sourceCatalogUnknown"));
  expect(screen.queryByText(i18n.t("appDetail.sourceOfficialTagManaged"))).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: i18n.t("appDetail.sourceEditRefButton") })).not.toBeInTheDocument();
});
