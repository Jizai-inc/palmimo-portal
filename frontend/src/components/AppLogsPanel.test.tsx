import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { getGetAppApiV1AppsNameGetMockHandler, getGetLogsApiV1AppsNameLogsGetMockHandler } from "@/api/generated/apps/apps.msw";
import type { AppDetailResponse } from "@/api/generated/models";
import { AppLogsPanel } from "@/components/AppLogsPanel";
import { renderWithRouter } from "@/test/render";
import { server } from "@/test/server";

function detail(overrides: Partial<AppDetailResponse> = {}): AppDetailResponse {
  return {
    autostart: false,
    bindings: {},
    broken_reason: null,
    description: "A test app.",
    devices: [],
    env: [],
    installed_at: 1_700_000_000,
    last_job: null,
    manifest: { devices: [], env: [], params: [] },
    name: "palmimo-teleop",
    params: {},
    source: { type: "git", url: "https://github.com/x/y", subdir: null, manifest: null, ref_kind: "tag", ref: "v1", commit: "abc123" },
    status: "running",
    ...overrides,
  };
}

describe("AppLogsPanel", () => {
  // useAppLogs auto-selects invocations[0] once the list loads (see useAppLogs.ts), so
  // "showing the current run" can no longer be read off `invocation === null` once that
  // effect fires -- a stale check would mislabel the live run's own logs as a past run.
  it("still labels the newest invocation as the current, live run once auto-selected", async () => {
    server.use(
      getGetAppApiV1AppsNameGetMockHandler(detail()),
      getGetLogsApiV1AppsNameLogsGetMockHandler({
        entries: [{ message: "line", timestamp: 2, invocation_id: "inv-1" }],
        invocations: [
          { id: "inv-1", started_at: 2 },
          { id: "inv-0", started_at: 1 },
        ],
        next_cursor: null,
      }),
    );

    renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);

    await waitFor(() => expect(screen.getByLabelText("Start")).toHaveValue("inv-1"));
    expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();
  });
});
