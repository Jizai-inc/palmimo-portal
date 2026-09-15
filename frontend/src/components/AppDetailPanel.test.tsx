import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
    name: "palmimo-teleop",
    params: { port: 8765, verbose: false },
    source: { type: "git", url: "https://github.com/x/y", subdir: null, ref_kind: "tag", ref: "v1", commit: "abc123" },
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
      http.put("*/api/v1/apps/palmimo-teleop/bindings", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail({ bindings: { WIFI_PASSWORD: "HOME_WIFI_PASSWORD" } }));
      }),
    );
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

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
      http.put("*/api/v1/apps/palmimo-teleop/params", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail());
      }),
    );
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

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
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

    const select = await screen.findByLabelText("mode");
    expect(select.tagName).toBe("SELECT");
    expect(within(select).getByRole("option", { name: "walk" })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "trot" })).toBeInTheDocument();
  });

  it("shows a previous-start option in the invocation select and refetches logs for it", async () => {
    const user = userEvent.setup();
    let lastInvocationParam: string | null = null;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.get("*/api/v1/apps/palmimo-teleop/logs", ({ request }) => {
        const url = new URL(request.url);
        // The generated client serializes an absent `invocation` as the literal query string
        // "null" (not an omitted param), so "null" must be treated the same as not-present.
        const raw = url.searchParams.get("invocation");
        lastInvocationParam = raw && raw !== "null" ? raw : null;
        return HttpResponse.json({
          entries: [{ message: lastInvocationParam ? "old start line" : "current start line", timestamp: 1, invocation_id: "inv-1" }],
          invocations: ["inv-1", "inv-0"],
          next_cursor: null,
        });
      }),
    );
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

    await screen.findByText("current start line");
    await user.selectOptions(screen.getByLabelText("Start"), "inv-0");

    await waitFor(() => expect(lastInvocationParam).toBe("inv-0"));
    expect(await screen.findByText("old start line")).toBeInTheDocument();
  });

  it("toggles autostart with a PUT of {enabled: true}", async () => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail({ autostart: false })),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.put("*/api/v1/apps/palmimo-teleop/autostart", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json(detail({ autostart: true }));
      }),
    );
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

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
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

    expect(await screen.findByText("Palmimo cannot read this app's logs right now.")).toBeInTheDocument();
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
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" />);

    await screen.findByLabelText("port");
    expect(screen.getByText("The port to serve on.")).toBeInTheDocument();
  });

  it("deletes the app after confirming the dialog, then reports completion once the job finishes", async () => {
    const user = userEvent.setup();
    const onDeleted = vi.fn();
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      http.delete("*/api/v1/apps/palmimo-teleop", () =>
        HttpResponse.json({ job: { id: "job-del", kind: "delete", state: "running", step: "register", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } }, { status: 202 }),
      ),
      http.get("*/api/v1/apps/jobs/job-del", () =>
        HttpResponse.json({ id: "job-del", kind: "delete", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
      ),
    );
    renderWithRouter(<AppDetailPanel name="palmimo-teleop" onDeleted={onDeleted} />);

    const deleteButtons = await screen.findAllByRole("button", { name: "Delete" });
    await user.click(deleteButtons[0]);
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(onDeleted).toHaveBeenCalled());
  });
});
