import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { focusManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getGetAppApiV1AppsNameGetMockHandler, getGetLogsApiV1AppsNameLogsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import type { AppDetailResponse } from "@/api/generated/models";
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

describe("AppDetailPanel", () => {
  // LogsSection always fetches on mount regardless of which behavior a test targets; give every
  // test a default empty response so only the logs-specific tests need to know its shape.
  beforeEach(() => {
    server.use(getGetLogsApiV1AppsNameLogsGetMockHandler({ entries: [], invocations: [], next_cursor: null }));
  });

  // Without this, saving bindings could accidentally submit a secret's value instead of its
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

  it("toggles autostart with a PUT of {enabled: true}", async () => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail({ autostart: false })),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.put("*/api/v1/apps/palmimo.teleop/autostart", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail({ autostart: true }));
      }),
    );
    renderWithRouter(<AppDetailPanel id="palmimo.teleop" />);

    const toggle = await screen.findByRole("switch");
    await user.click(toggle);

    await waitFor(() => expect(putBody).toEqual({ enabled: true }));
  });

  // Without this, a permission-denied journal read would render nothing (or leak raw JSON)
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

  // Without this, an operator would see the raw manifest device id ("motor_display") instead of
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

  // Without this, an operator would see a param's name and type but not the manifest author's
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
});
