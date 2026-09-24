import { act, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getGetAppApiV1AppsNameGetMockHandler, getGetLogsApiV1AppsNameLogsGetMockHandler } from "@/api/generated/apps/apps.msw";
import type { AppDetailResponse } from "@/api/generated/models";
import { AppLogsPanel } from "@/components/AppLogsPanel";
import { LOG_POLL_INTERVAL_MS } from "@/lib/useAppLogs";
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
  // isCurrentInvocation comes from useAppLogs' own "not pinned to an older run" state (see
  // useAppLogs.ts), not from comparing ids captured at an earlier render.
  it("labels the newest invocation as the current, live run", async () => {
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

  describe("following a run that restarts under a new invocation", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    // Without following `invocations[0]` on every poll (rather than fixing the choice once), the
    // page would keep showing the old run's logs and mislabel it as "previous run", even though
    // the app has since restarted under a new invocation and is live again.
    it("keeps showing the current, live run once the unit restarts", async () => {
      let restarted = false;
      server.use(
        getGetAppApiV1AppsNameGetMockHandler(detail()),
        http.get("*/api/v1/apps/palmimo-teleop/logs", ({ request }) => {
          const url = new URL(request.url);
          const raw = url.searchParams.get("invocation");
          const requested = raw && raw !== "null" ? raw : null;
          const invocations = restarted
            ? [{ id: "inv-2", started_at: 2 }, { id: "inv-1", started_at: 1 }]
            : [{ id: "inv-1", started_at: 1 }];
          const allEntries = [
            { message: "old run line", timestamp: 1, invocation_id: "inv-1" },
            ...(restarted ? [{ message: "new run line", timestamp: 2, invocation_id: "inv-2" }] : []),
          ];
          const entries = requested ? allEntries.filter((entry) => entry.invocation_id === requested) : allEntries;
          return HttpResponse.json({ entries, invocations, next_cursor: null });
        }),
      );

      renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);
      await waitFor(() => expect(screen.getByText("old run line")).toBeInTheDocument());
      expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();

      restarted = true;
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));

      await waitFor(() => expect(screen.getByText("new run line")).toBeInTheDocument());
      expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();
    });
  });
});
