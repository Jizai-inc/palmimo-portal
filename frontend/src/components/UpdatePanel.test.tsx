import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UpdateStatusResponse } from "@/api/generated/models";
import { getGetStatusApiV1UpdateStatusGetMockHandler } from "@/api/generated/update/update.msw";
import { UpdatePanel, __getRestartObservationForTests, __resetRestartObservationForTests } from "@/components/UpdatePanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function jsonError(status: number, code: string, params: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, params } }, { status });
}

const IDLE_JOB = {
  kind: "update",
  state: "idle",
  target: null,
  step: null,
  error: null,
  started_at: null,
  finished_at: null,
  restarting_at: null,
};

const BASE_STATUS: UpdateStatusResponse = {
  installed: { tag: "v1.0.0", commit: "abc1234" },
  latest: { tag: "v1.0.0", name: "v1.0.0", published_at: "2026-01-01T00:00:00Z", html_url: "https://example.test/v1" },
  checked_at: Math.floor(Date.now() / 1000),
  update_available: false,
  previous_tag: null,
  retry_available: false,
  job: IDLE_JOB,
};

const UPDATE_AVAILABLE_STATUS: UpdateStatusResponse = {
  ...BASE_STATUS,
  latest: { tag: "v2.0.0", name: "v2.0.0", published_at: "2026-02-01T00:00:00Z", html_url: "https://example.test/v2" },
  update_available: true,
};

function useStatus(status: UpdateStatusResponse) {
  server.use(getGetStatusApiV1UpdateStatusGetMockHandler(status));
}

const RESTART_TIMED_OUT_TEXT =
  "Palmimo has not come back yet. Power-cycle the device; if the Portal still does not start, roll back over SSH.";

/** A `system/status` response that always succeeds, so the fail-then-succeed restart-poll path (covered elsewhere) never fires -- only the restart-wait deadline under test can resolve the screen. */
function systemStatusAlwaysOkHandler() {
  return http.get("*/api/v1/system/status", () =>
    HttpResponse.json({
      state: "connected",
      hostname: "palmimo-1234",
      auth_state: "set",
      device_id: "1234",
      versions: { portal: "0.2.0", sdk: null },
      last_wifi_attempt: null,
      adapters: "fake",
      state_dir: "/tmp",
    }),
  );
}

describe("UpdatePanel", () => {
  it("renders the installed version, latest release, and last checked date", async () => {
    useStatus(BASE_STATUS);
    renderWithProviders(<UpdatePanel />);

    expect((await screen.findAllByText("v1.0.0")).length).toBeGreaterThan(0);
    expect(screen.getByText("Up to date")).toBeInTheDocument();
  });

  it("enables the update button when an update is available, and applies it via the confirm dialog", async () => {
    const user = userEvent.setup();
    useStatus(UPDATE_AVAILABLE_STATUS);
    let applyBody: unknown;
    server.use(
      http.post("*/api/v1/update/apply", async ({ request }) => {
        applyBody = await request.json();
        return HttpResponse.json(
          { ...UPDATE_AVAILABLE_STATUS, job: { ...IDLE_JOB, state: "running", target: "v2.0.0", step: "fetch" } },
          { status: 202 },
        );
      }),
    );
    renderWithProviders(<UpdatePanel />);

    const updateButton = await screen.findByRole("button", { name: "Update to v2.0.0" });
    expect(updateButton).not.toBeDisabled();
    await user.click(updateButton);

    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText("Update to v2.0.0?")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    await waitFor(() => expect(applyBody).toEqual({ tag: "v2.0.0" }));
  });

  it("disables the update button when no update is available", async () => {
    useStatus(BASE_STATUS);
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByRole("button", { name: "Update to v1.0.0" })).toBeDisabled();
  });

  it("shows progress with the step label while the job is running", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      job: { ...IDLE_JOB, state: "running", target: "v2.0.0", step: "checkout" },
    });
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByText("Updating… (Switching versions)")).toBeInTheDocument();
  });

  it("polls system/status while restarting and calls onRestarted once a poll fails then succeeds", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null },
    });
    const onRestarted = vi.fn();
    let statusCallCount = 0;
    server.use(
      http.get("*/api/v1/system/status", () => {
        statusCallCount += 1;
        if (statusCallCount === 1) {
          return HttpResponse.error();
        }
        return HttpResponse.json({
          state: "connected",
          hostname: "palmimo-1234",
          auth_state: "set",
          device_id: "1234",
          versions: { portal: "0.2.0", sdk: null },
          last_wifi_attempt: null,
          adapters: "fake",
          state_dir: "/tmp",
        });
      }),
    );
    renderWithProviders(<UpdatePanel onRestarted={onRestarted} restartPollIntervalMs={20} />);

    expect(await screen.findByText("Restarting…")).toBeInTheDocument();
    await waitFor(() => expect(statusCallCount).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(onRestarted).toHaveBeenCalledTimes(1));
  });

  it(
    "calls onRestarted once update/status itself observes job done after restarting, even when system/status never failed",
    async () => {
      // A restart faster than one JOB_POLL_INTERVAL_MS tick: the very next
      // update/status poll already shows "done" on the new tag, with
      // system/status having stayed reachable throughout -- the
      // fail-then-succeed system/status path (previous test) would never
      // fire here, so onRestarted must come from this poll instead.
      const onRestarted = vi.fn();
      let updateStatusCallCount = 0;
      server.use(
        http.get("*/api/v1/update/status", () => {
          updateStatusCallCount += 1;
          if (updateStatusCallCount === 1) {
            return HttpResponse.json({
              ...UPDATE_AVAILABLE_STATUS,
              job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null },
            });
          }
          return HttpResponse.json({
            ...UPDATE_AVAILABLE_STATUS,
            installed: { tag: "v2.0.0", commit: "def5678" },
            job: { ...IDLE_JOB, state: "done", target: "v2.0.0", step: null },
          });
        }),
        http.get("*/api/v1/system/status", () =>
          HttpResponse.json({
            state: "connected",
            hostname: "palmimo-1234",
            auth_state: "set",
            device_id: "1234",
            versions: { portal: "0.2.0", sdk: null },
            last_wifi_attempt: null,
            adapters: "fake",
            state_dir: "/tmp",
          }),
        ),
      );

      renderWithProviders(<UpdatePanel onRestarted={onRestarted} restartPollIntervalMs={20} />);

      expect(await screen.findByText("Restarting…")).toBeInTheDocument();
      await waitFor(() => expect(onRestarted).toHaveBeenCalledTimes(1), { timeout: 8000 });
    },
    10_000,
  );

  it("shows a destructive error and offers rollback when the job failed", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      // A sync failure typically lands with HEAD already checked out onto
      // the target (see core/update.py's start_apply docstring) -- distinct
      // from previous_tag, so the rollback card renders normally.
      installed: { tag: "v2.0.0", commit: "def5678" },
      previous_tag: "v1.0.0",
      job: { kind: "update", state: "failed", target: "v2.0.0", step: "sync", error: "uv sync failed", started_at: 1, finished_at: 2, restarting_at: null },
    });
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByText(/uv sync failed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go back to v1.0.0" })).toBeInTheDocument();
  });

  it("rolls back via its own confirm dialog", async () => {
    const user = userEvent.setup();
    useStatus({ ...BASE_STATUS, previous_tag: "v0.9.0" });
    let rollbackCalled = false;
    server.use(
      http.post("*/api/v1/update/rollback", () => {
        rollbackCalled = true;
        return HttpResponse.json(
          { ...BASE_STATUS, previous_tag: "v0.9.0", job: { ...IDLE_JOB, state: "running", kind: "rollback", target: "v0.9.0" } },
          { status: 202 },
        );
      }),
    );
    renderWithProviders(<UpdatePanel />);

    await user.click(await screen.findByRole("button", { name: "Go back to v0.9.0" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Go back" }));

    await waitFor(() => expect(rollbackCalled).toBe(true));
  });

  it("hides the rollback card when previous_tag equals the installed tag", async () => {
    useStatus({ ...BASE_STATUS, previous_tag: "v1.0.0" }); // BASE_STATUS.installed.tag is also "v1.0.0"
    renderWithProviders(<UpdatePanel />);

    await screen.findByText("Up to date");
    expect(screen.queryByRole("button", { name: "Go back to v1.0.0" })).not.toBeInTheDocument();
  });

  it("auto-checks once when the last check is stale", async () => {
    const staleStatus: UpdateStatusResponse = { ...BASE_STATUS, checked_at: Math.floor(Date.now() / 1000) - 3700 };
    useStatus(staleStatus);
    let checkCalled = 0;
    server.use(
      http.post("*/api/v1/update/check", () => {
        checkCalled += 1;
        return HttpResponse.json(BASE_STATUS);
      }),
    );
    renderWithProviders(<UpdatePanel />);

    await screen.findAllByText("v1.0.0");
    await waitFor(() => expect(checkCalled).toBe(1));
  });

  it("does not auto-check when the last check is fresh", async () => {
    useStatus(BASE_STATUS);
    let checkCalled = 0;
    server.use(
      http.post("*/api/v1/update/check", () => {
        checkCalled += 1;
        return HttpResponse.json(BASE_STATUS);
      }),
    );
    renderWithProviders(<UpdatePanel />);

    await screen.findAllByText("v1.0.0");
    await delay(20);
    expect(checkCalled).toBe(0);
  });

  it("ignores a 429 from the auto-check", async () => {
    const staleStatus: UpdateStatusResponse = { ...BASE_STATUS, checked_at: Math.floor(Date.now() / 1000) - 3700 };
    useStatus(staleStatus);
    server.use(
      http.post("*/api/v1/update/check", () => jsonError(429, "update_check_rate_limited", { retry_after_seconds: 30 })),
    );
    renderWithProviders(<UpdatePanel />);

    await screen.findAllByText("v1.0.0");
    // No unhandled-rejection / rendered error: the component just stays put.
    expect(screen.queryByText("You just checked for updates. Try again in 30 seconds.")).not.toBeInTheDocument();
  });

  it("updates the status text from the check mutation's own response, via the real click path", async () => {
    const user = userEvent.setup();
    useStatus(BASE_STATUS);
    server.use(
      http.post("*/api/v1/update/check", () =>
        HttpResponse.json({
          ...UPDATE_AVAILABLE_STATUS,
          checked_at: Math.floor(Date.now() / 1000),
        }),
      ),
    );
    renderWithProviders(<UpdatePanel />);

    await screen.findByText("Up to date");
    await user.click(screen.getByRole("button", { name: "Check now" }));

    // The GET handler never changed -- this can only come from the
    // mutation's own response being consumed (setQueryData), not a refetch.
    expect(await screen.findByText("A new version is available")).toBeInTheDocument();
  });

  it("shows the running/progress UI from the apply mutation's own response, without the status GET ever reporting running", async () => {
    const user = userEvent.setup();
    useStatus(UPDATE_AVAILABLE_STATUS); // the GET handler keeps answering this, unchanged, for the whole test
    server.use(
      http.post("*/api/v1/update/apply", async ({ request }) => {
        void (await request.json());
        return HttpResponse.json(
          { ...UPDATE_AVAILABLE_STATUS, job: { ...IDLE_JOB, state: "running", target: "v2.0.0", step: "fetch" } },
          { status: 202 },
        );
      }),
    );
    renderWithProviders(<UpdatePanel />);

    const updateButton = await screen.findByRole("button", { name: "Update to v2.0.0" });
    await user.click(updateButton);
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Update" }));

    expect(await screen.findByText("Updating… (Fetching)")).toBeInTheDocument();
  });

  it("offers a retry button when the job failed at the target that is still latest, and re-applies it", async () => {
    const user = userEvent.setup();
    useStatus({
      ...BASE_STATUS,
      job: { kind: "update", state: "failed", target: "v1.0.0", step: "sync", error: "uv sync failed", started_at: 1, finished_at: 2, restarting_at: null },
      retry_available: true,
    });
    let applyBody: unknown;
    server.use(
      http.post("*/api/v1/update/apply", async ({ request }) => {
        applyBody = await request.json();
        return HttpResponse.json(
          { ...BASE_STATUS, job: { kind: "update", state: "running", target: "v1.0.0", step: "fetch", error: null, started_at: 3, finished_at: null } },
          { status: 202 },
        );
      }),
    );
    renderWithProviders(<UpdatePanel />);

    const retryButton = await screen.findByRole("button", { name: "Retry" });
    await user.click(retryButton);

    await waitFor(() => expect(applyBody).toEqual({ tag: "v1.0.0" }));
  });

  it("does not offer a retry button when the job failed but retry is not available", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      previous_tag: "v1.0.0",
      job: { kind: "update", state: "failed", target: "v2.0.0", step: "sync", error: "uv sync failed", started_at: 1, finished_at: 2, restarting_at: null },
      retry_available: false,
    });
    renderWithProviders(<UpdatePanel />);

    await screen.findByText(/uv sync failed/);
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });

  it("renders the manual check-now failure", async () => {
    const user = userEvent.setup();
    useStatus(BASE_STATUS);
    server.use(http.post("*/api/v1/update/check", () => jsonError(502, "release_source_unavailable")));
    renderWithProviders(<UpdatePanel />);

    await screen.findByText("Up to date");
    await user.click(screen.getByRole("button", { name: "Check now" }));

    expect(await screen.findByText("Palmimo could not reach the update service. Try again shortly.")).toBeInTheDocument();
  });

  it("stays silent on a manual check-now 429", async () => {
    const user = userEvent.setup();
    useStatus(BASE_STATUS);
    server.use(
      http.post("*/api/v1/update/check", () => jsonError(429, "update_check_rate_limited", { retry_after_seconds: 30 })),
    );
    renderWithProviders(<UpdatePanel />);

    await screen.findByText("Up to date");
    await user.click(screen.getByRole("button", { name: "Check now" }));

    expect(screen.queryByText("You just checked for updates. Try again in 30 seconds.")).not.toBeInTheDocument();
  });

  it("labels the failed step through stepLabel rather than the raw backend step name", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      installed: { tag: "v2.0.0", commit: "def5678" },
      job: {
        kind: "update",
        state: "failed",
        target: "v2.0.0",
        step: "restart",
        error: "logind down",
        started_at: 1,
        finished_at: 2,
        restarting_at: 1.5,
      },
    });
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByText(/\(Restarting the service\)/)).toBeInTheDocument();
    expect(screen.queryByText(/\(restart\)/)).not.toBeInTheDocument();
  });

  it("renders 'Updated to <tag>' for a done job", async () => {
    useStatus({
      ...BASE_STATUS,
      job: { kind: "update", state: "done", target: "v1.0.0", step: null, error: null, started_at: 1, finished_at: 2, restarting_at: 1.2 },
    });
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByText("Updated to v1.0.0")).toBeInTheDocument();
  });

  it("disables the update button when latest is null", async () => {
    useStatus({ ...BASE_STATUS, latest: null, update_available: false });
    renderWithProviders(<UpdatePanel />);

    expect(await screen.findByRole("button", { name: /^Update to\s*$/ })).toBeDisabled();
  });

  it("shows restart-timed-out guidance with a reopen action after restartMaxWaitMs elapses", async () => {
    useStatus({
      ...UPDATE_AVAILABLE_STATUS,
      job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null },
    });
    // system/status never succeeds -- the restart never actually lands, so
    // the fail-then-succeed poll path never fires either; only the
    // dedicated restart-wait timeout can resolve this screen.
    server.use(http.get("*/api/v1/system/status", () => HttpResponse.error()));

    renderWithProviders(<UpdatePanel restartPollIntervalMs={10_000} restartMaxWaitMs={30} />);

    expect(await screen.findByText("Restarting…")).toBeInTheDocument();
    expect(await screen.findByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reopen the Portal" })).toBeInTheDocument();
  });

  // The restart-wait deadline is anchored to *this browser's* clock at the
  // moment it first observes the restarting job, not to `job.restarting_at`
  // (a server/device epoch-seconds timestamp). The Pi has no RTC: right
  // after boot, before NTP settles, its clock can be minutes off from the
  // browser's. These pin the fix -- a device clock skewed either direction
  // must not perturb the client-side 10-minute UI budget -- plus the
  // module-level anchor's persistence across an unmount/remount, which is
  // load-bearing: the *only* other path to this guidance is the server's
  // own 600s expiry (core/update.py), and that only ever runs inside
  // `GET /update/status` handling and at boot, so it cannot fire on its own
  // while nothing is polling it. All of these use fake timers so the real
  // 10-minute wait is never actually paid.
  describe("restart-wait deadline anchoring (client clock)", () => {
    beforeEach(() => {
      // `shouldAdvanceTime` lets real async work (MSW's fetch interception)
      // keep resolving via the real clock while `advanceTimersByTimeAsync`
      // still drives the virtual 10-minute jumps below.
      vi.useFakeTimers({ shouldAdvanceTime: true });
      // The restart-observation anchor is module state (deliberately, so it
      // survives a real unmount/remount) -- so unlike component state, it
      // does not reset itself between test cases and must be reset by hand.
      __resetRestartObservationForTests();
    });

    afterEach(() => {
      vi.useRealTimers();
      __resetRestartObservationForTests();
    });

    it("does not show timed-out guidance immediately when the device clock is far behind, and shows it after 10 minutes of client time", async () => {
      const twoHoursAgoSeconds = Date.now() / 1000 - 2 * 60 * 60;
      useStatus({
        ...UPDATE_AVAILABLE_STATUS,
        job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: twoHoursAgoSeconds },
      });
      server.use(systemStatusAlwaysOkHandler());

      renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();

      // Under a `restarting_at * 1000 + 10min` deadline this would already
      // be ~1h50m in the past -- i.e. due immediately -- so give any such
      // already-due timer a tick to fire before asserting it did not.
      await act(() => vi.advanceTimersByTimeAsync(1));
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

      // Advancing the client's own clock past the 10-minute budget (plus a
      // comfortable margin against timer-scheduling jitter) is what
      // triggers the guidance.
      await act(() => vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 5_000));
      expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
    });

    it("shows timed-out guidance after 10 minutes of client time (not 2h10m) when the device clock is far ahead", async () => {
      const twoHoursFromNowSeconds = Date.now() / 1000 + 2 * 60 * 60;
      useStatus({
        ...UPDATE_AVAILABLE_STATUS,
        job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: twoHoursFromNowSeconds },
      });
      server.use(systemStatusAlwaysOkHandler());

      renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();

      // Comfortably short of the 10-minute client budget: still waiting.
      await act(() => vi.advanceTimersByTimeAsync(9 * 60 * 1000 + 30_000));
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

      // Comfortably past the 10-minute client budget -- but nowhere near the
      // ~2h10m a stale server-epoch deadline would instead require.
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
    });

    it("shows the failure UI immediately once a poll observes a server-side failed job, before the client deadline elapses", async () => {
      let updateStatusCallCount = 0;
      server.use(
        http.get("*/api/v1/update/status", () => {
          updateStatusCallCount += 1;
          if (updateStatusCallCount === 1) {
            return HttpResponse.json({
              ...UPDATE_AVAILABLE_STATUS,
              job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: Date.now() / 1000 },
            });
          }
          return HttpResponse.json({
            ...UPDATE_AVAILABLE_STATUS,
            job: {
              kind: "update",
              state: "failed",
              target: "v2.0.0",
              step: "restart",
              error: "service failed to come back",
              started_at: 1,
              finished_at: 2,
              restarting_at: 1.5,
            },
          });
        }),
        systemStatusAlwaysOkHandler(),
      );

      renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();

      // The next `update/status` poll (JOB_POLL_INTERVAL_MS, well inside the
      // 10-minute deadline) reports `failed` -- the failure UI must win
      // immediately, without waiting out the client-side timeout.
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(await screen.findByText(/The update failed/)).toBeInTheDocument();
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();
    });

    it("resets the 10-minute budget (and rekeys the module-level observation) when a new restart supersedes the one being waited on", async () => {
      const mountedAtMs = Date.now();
      const firstRestartingAt = mountedAtMs / 1000;
      const secondRestartingAt = firstRestartingAt + 5 * 60;
      server.use(
        http.get("*/api/v1/update/status", () => {
          // The first restart is superseded by a second one ~60s in -- e.g.
          // a retry after the operator power-cycled the device by hand.
          const restartingAt = Date.now() - mountedAtMs < 60_000 ? firstRestartingAt : secondRestartingAt;
          return HttpResponse.json({
            ...UPDATE_AVAILABLE_STATUS,
            job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: restartingAt },
          });
        }),
        systemStatusAlwaysOkHandler(),
      );

      renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();
      const firstObservation = __getRestartObservationForTests();
      expect(firstObservation?.key).toBe(firstRestartingAt);

      // Cross the 60s mark: the next `update/status` poll (every
      // JOB_POLL_INTERVAL_MS) now reports the second restart's
      // `restarting_at`, which must rekey the module-level observation and
      // reset the 10-minute budget to count fresh from this moment.
      await act(() => vi.advanceTimersByTimeAsync(65_000));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();
      const secondObservation = __getRestartObservationForTests();
      expect(secondObservation?.key).toBe(secondRestartingAt);
      expect(secondObservation?.key).not.toBe(firstObservation?.key);
      expect(secondObservation?.observedAtMs).toBeGreaterThan(firstObservation?.observedAtMs ?? 0);

      // Comfortably short of the reset deadline (~10 minutes after the
      // rekey, i.e. ~11 minutes from mount): under a reset budget, still
      // waiting.
      await act(() => vi.advanceTimersByTimeAsync(500_000 - 65_000));
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

      // Comfortably past the reset deadline (~700s from mount): only a
      // budget that actually reset on the second restart fires this early --
      // an unreset first-restart budget wouldn't have fired until ~600s
      // *unless* the rekey never happened, which the assertions above
      // already ruled out directly.
      await act(() => vi.advanceTimersByTimeAsync(700_000 - 500_000));
      expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
    });


    it("clears stale timed-out guidance when a new restart (no intermediate non-restarting poll) supersedes the one that timed out", async () => {
      const firstRestartingAt = Date.now() / 1000;
      let secondRestartingAt: number | null = null;
      let updateStatusCallCount = 0;
      server.use(
        http.get("*/api/v1/update/status", () => {
          updateStatusCallCount += 1;
          // The first poll (and every poll until the test flips
          // `secondRestartingAt`) reports the first restart; once flipped,
          // every subsequent poll reports the second restart directly --
          // i.e. `job.state` is "restarting" on both sides of the seam,
          // with no intervening "done"/"failed"/"idle" poll in between (a
          // missed transition).
          const restartingAt = secondRestartingAt ?? firstRestartingAt;
          return HttpResponse.json({
            ...UPDATE_AVAILABLE_STATUS,
            job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: restartingAt },
          });
        }),
        systemStatusAlwaysOkHandler(),
      );

      renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
      await act(() => vi.advanceTimersByTimeAsync(0));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();

      // Run the first restart's own budget all the way out: the power-cycle
      // guidance is now visible.
      await act(() => vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 5_000));
      expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
      expect(updateStatusCallCount).toBeGreaterThan(0);

      // A second, distinct restart supersedes the first -- e.g. the
      // operator power-cycled the device by hand and a fresh update job
      // picked it up -- reported directly as "restarting" with a new
      // `restarting_at`, never passing through "done"/"failed"/"idle" on
      // the way. Without the fix, the stale `restartTimedOut` flag would
      // keep the guidance on screen through this transition.
      secondRestartingAt = firstRestartingAt + 20 * 60;
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(screen.getByText("Restarting…")).toBeInTheDocument();
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

      // The fresh 10-minute budget applies to the new restart, counted from
      // when it was first observed, not the old restart's already-spent one.
      await act(() => vi.advanceTimersByTimeAsync(9 * 60 * 1000 + 30_000));
      expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
    });
    // The client-side timeout is anchored to a module-level record
    // (`restartObservation` in UpdatePanel.tsx) specifically so it survives
    // this component unmounting and remounting -- e.g. the operator
    // navigates to another screen and back while a restart is in progress.
    // That persistence is load-bearing, not cosmetic: the server's own 600s
    // expiry (core/update.py) that would otherwise flip a stuck job to
    // `failed` only ever runs inside `GET /update/status` handling and at
    // boot -- it cannot fire on its own while nothing is polling it. In the
    // one scenario this guidance exists for (a crash-loop after
    // `restart_portal()` succeeds, so the fail-then-succeed poll elsewhere
    // in this component never observes a transition), the client-side
    // timeout is the *only* path to the power-cycle guidance. Arming a
    // fresh budget on every mount -- the regression this describe block
    // guards against -- would let a user who bounces between screens
    // postpone that guidance indefinitely.
    describe("survives an unmount/remount", () => {
      // Both cases below keep MSW serving a healthy `restarting` status
      // response across the unmount -- the status *fetch* itself keeps
      // succeeding, only the actual restart never lands. That is a
      // deliberate simplification: a fully dead server (the fetch itself
      // failing) takes a different code path entirely, rendering the
      // top-level `ApiErrorAlert` instead of this component's restarting
      // UI. That path is unrelated to this timeout and out of scope here.

      it("(a) remount after 4 minutes elapsed shows guidance after 6 more minutes, not a fresh 10", async () => {
        const restartingAtSeconds = Date.now() / 1000;
        useStatus({
          ...UPDATE_AVAILABLE_STATUS,
          job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: restartingAtSeconds },
        });
        server.use(systemStatusAlwaysOkHandler());

        const first = renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
        await act(() => vi.advanceTimersByTimeAsync(0));
        expect(screen.getByText("Restarting…")).toBeInTheDocument();

        // 4 minutes elapse while mounted, then the operator navigates away.
        await act(() => vi.advanceTimersByTimeAsync(4 * 60 * 1000));
        first.unmount();

        // Remount: a fresh component instance and a fresh QueryClient (as a
        // real route remount would also get), observing the same restart
        // (same `restarting_at`) via a freshly-registered handler.
        useStatus({
          ...UPDATE_AVAILABLE_STATUS,
          job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: restartingAtSeconds },
        });
        renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
        await act(() => vi.advanceTimersByTimeAsync(0));
        expect(screen.getByText("Restarting…")).toBeInTheDocument();
        expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

        // Comfortably short of the ~6 remaining minutes (10 total): still
        // waiting. A fresh-10-minutes-on-remount bug would also still be
        // waiting here, so this alone would not discriminate the fix -- the
        // next assertion does.
        await act(() => vi.advanceTimersByTimeAsync(4 * 60 * 1000));
        expect(screen.queryByText(RESTART_TIMED_OUT_TEXT)).not.toBeInTheDocument();

        // Comfortably past the ~6 remaining minutes (10 total from the
        // *original* observation): a fresh-10-minutes-on-remount bug would
        // still be ~3 minutes short of its own deadline here.
        await act(() => vi.advanceTimersByTimeAsync(150_000));
        expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
      });

      it("(b) remount after the budget already expired while unmounted shows guidance immediately", async () => {
        const restartingAtSeconds = Date.now() / 1000;
        useStatus({
          ...UPDATE_AVAILABLE_STATUS,
          job: { ...IDLE_JOB, state: "restarting", target: "v2.0.0", step: null, restarting_at: restartingAtSeconds },
        });
        server.use(systemStatusAlwaysOkHandler());

        const first = renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
        await act(() => vi.advanceTimersByTimeAsync(0));
        expect(screen.getByText("Restarting…")).toBeInTheDocument();
        first.unmount();

        // The entire 11-minute budget overrun happens while unmounted --
        // the essence of the crash-loop scenario this guidance exists for:
        // the backend never actually comes back, nothing here ever calls
        // onRestarted, and the operator happens to be looking at a
        // different screen while the budget quietly runs out.
        await act(() => vi.advanceTimersByTimeAsync(11 * 60 * 1000));

        renderWithProviders(<UpdatePanel restartPollIntervalMs={20 * 60 * 1000} />);
        await act(() => vi.advanceTimersByTimeAsync(0));
        expect(screen.getByText(RESTART_TIMED_OUT_TEXT)).toBeInTheDocument();
      });
    });
  });
});
