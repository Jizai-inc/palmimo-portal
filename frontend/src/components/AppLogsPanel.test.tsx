import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
        entries: [{ message: "line", timestamp: 2, invocation_id: "11111111111111111111111111111111" }],
        invocations: [
          { id: "11111111111111111111111111111111", started_at: 2 },
          { id: "00000000000000000000000000000000", started_at: 1 },
        ],
        next_cursor: null,
      }),
    );

    renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);

    await waitFor(() => expect(screen.getByLabelText("Start")).toHaveValue("11111111111111111111111111111111"));
    expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();
  });

  describe("polling and selection behavior", () => {
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
            ? [{ id: "22222222222222222222222222222222", started_at: 2 }, { id: "11111111111111111111111111111111", started_at: 1 }]
            : [{ id: "11111111111111111111111111111111", started_at: 1 }];
          const allEntries = [
            { message: "old run line", timestamp: 1, invocation_id: "11111111111111111111111111111111" },
            ...(restarted ? [{ message: "new run line", timestamp: 2, invocation_id: "22222222222222222222222222222222" }] : []),
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

    // Picking an older run and then re-picking the newest one from the dropdown must return to
    // following it, not merely match its id once -- otherwise a later restart (a third
    // invocation) is missed just as if the newest one had never been re-selected.
    it("returns to following once the newest invocation is reselected", async () => {
      const user = userEvent.setup();
      let restarted = false;
      server.use(
        getGetAppApiV1AppsNameGetMockHandler(detail()),
        http.get("*/api/v1/apps/palmimo-teleop/logs", ({ request }) => {
          const url = new URL(request.url);
          const raw = url.searchParams.get("invocation");
          const requested = raw && raw !== "null" ? raw : null;
          const invocations = restarted
            ? [{ id: "cccccccccccccccccccccccccccccccc", started_at: 3 }, { id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", started_at: 2 }, { id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", started_at: 1 }]
            : [{ id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", started_at: 2 }, { id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", started_at: 1 }];
          const allEntries = [
            { message: "w line", timestamp: 1, invocation_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" },
            { message: "x line", timestamp: 2, invocation_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" },
            ...(restarted ? [{ message: "y line", timestamp: 3, invocation_id: "cccccccccccccccccccccccccccccccc" }] : []),
          ];
          const entries = requested ? allEntries.filter((entry) => entry.invocation_id === requested) : allEntries;
          return HttpResponse.json({ entries, invocations, next_cursor: null });
        }),
      );

      renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);
      await waitFor(() => expect(screen.getByLabelText("Start")).toHaveValue("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"));

      await user.selectOptions(screen.getByLabelText("Start"), "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
      await waitFor(() => expect(screen.getByText("w line")).toBeInTheDocument());
      expect(screen.getByText("A previous run.")).toBeInTheDocument();

      await user.selectOptions(screen.getByLabelText("Start"), "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
      await waitFor(() => expect(screen.getByText("x line")).toBeInTheDocument());
      expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();

      restarted = true;
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));

      await waitFor(() => expect(screen.getByText("y line")).toBeInTheDocument());
      expect(screen.getByText("Current run. Updates every 2 seconds.")).toBeInTheDocument();
    });

    // `_list_invocations` answers `[]` when its own journalctl call fails (see journal.py) --
    // that must not be read as "this unit has no runs", or the view loses the run it was already
    // following and falls back to an unfiltered (potentially multi-invocation) request.
    it("keeps following the same invocation through a transient empty invocation listing", async () => {
      let listingFailed = false;
      const requestedInvocations: (string | null)[] = [];
      server.use(
        getGetAppApiV1AppsNameGetMockHandler(detail()),
        http.get("*/api/v1/apps/palmimo-teleop/logs", ({ request }) => {
          const url = new URL(request.url);
          const raw = url.searchParams.get("invocation");
          const requested = raw && raw !== "null" ? raw : null;
          requestedInvocations.push(requested);
          const allEntries = [
            { message: "old line", timestamp: 1, invocation_id: "11111111111111111111111111111111" },
            { message: "new line", timestamp: 2, invocation_id: "22222222222222222222222222222222" },
          ];
          const entries = requested ? allEntries.filter((entry) => entry.invocation_id === requested) : allEntries;
          return HttpResponse.json({
            entries,
            invocations: listingFailed ? [] : [{ id: "22222222222222222222222222222222", started_at: 2 }, { id: "11111111111111111111111111111111", started_at: 1 }],
            next_cursor: null,
          });
        }),
      );

      renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);
      await waitFor(() => expect(screen.getByText("new line")).toBeInTheDocument());

      listingFailed = true;
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));

      // The mount's own first request is legitimately unfiltered (nothing followed yet); no
      // request after that should ever drop the invocation filter again.
      expect(requestedInvocations.slice(1)).not.toContain(null);
      expect(screen.getAllByText("new line").length).toBeGreaterThan(0);
      expect(screen.queryByText("old line")).not.toBeInTheDocument();
    });

    // A cursor is only valid for the invocation it was issued against -- pairing a stale one
    // (from the invocation just abandoned) with the new invocation would ask the backend to page
    // through a journal window that id was never fetched from.
    it("never sends a cursor from an abandoned invocation together with the new one", async () => {
      let restarted = false;
      const seen: { invocation: string | null; cursor: string | null }[] = [];
      server.use(
        getGetAppApiV1AppsNameGetMockHandler(detail()),
        http.get("*/api/v1/apps/palmimo-teleop/logs", ({ request }) => {
          const url = new URL(request.url);
          const rawInvocation = url.searchParams.get("invocation");
          const requested = rawInvocation && rawInvocation !== "null" ? rawInvocation : null;
          const rawCursor = url.searchParams.get("cursor");
          const cursor = rawCursor && rawCursor !== "null" ? rawCursor : null;
          seen.push({ invocation: requested, cursor });
          const invocations = restarted
            ? [{ id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", started_at: 2 }, { id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", started_at: 1 }]
            : [{ id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", started_at: 1 }];
          const entries =
            requested === "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
              ? [{ message: "x line", timestamp: 2, invocation_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" }]
              : [{ message: "w line", timestamp: 1, invocation_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }];
          return HttpResponse.json({ entries, invocations, next_cursor: "cursor-w" });
        }),
      );

      renderWithRouter(<AppLogsPanel name="palmimo-teleop" />);
      await waitFor(() => expect(screen.getByText("w line")).toBeInTheDocument());

      restarted = true;
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));
      await act(() => vi.advanceTimersByTimeAsync(LOG_POLL_INTERVAL_MS));

      await waitFor(() => expect(screen.getByText("x line")).toBeInTheDocument());

      const firstRequestForX = seen.find((request) => request.invocation === "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
      expect(firstRequestForX?.cursor).toBeNull();
    });
  });
});
