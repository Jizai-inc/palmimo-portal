import { act, screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppJobDialog } from "@/components/AppJobDialog";
import { renderWithProviders } from "@/test/render";
import { startJob } from "@/test/startJob";
import { server } from "@/test/server";

function jsonError(status: number, code: string) {
  return HttpResponse.json({ error: { code, params: {} } }, { status });
}

describe("AppJobDialog", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a terminal message and stops polling when the job endpoint answers 404", async () => {
    let callCount = 0;
    server.use(
      http.get("*/api/v1/apps/jobs/:jobId", () => {
        callCount += 1;
        return jsonError(404, "job_not_found");
      }),
    );
    const onDone = vi.fn();

    renderWithProviders(<AppJobDialog jobId="job-1" title="palmimo-teleop" onClose={vi.fn()} onDone={onDone} />);
    await act(() => vi.advanceTimersByTimeAsync(0));

    expect(await screen.findByText("This job is no longer known to the Portal")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close" })).toBeInTheDocument();
    expect(onDone).not.toHaveBeenCalled();

    const callsAfterFirstError = callCount;
    // Without the refetchInterval fix, react-query keeps the previous (nonexistent) cached
    // "running" data around on a fetch error and keeps polling forever.
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(callCount).toBe(callsAfterFirstError);
  });

  // git stderr tail instead of the same "check your PAT's scope" guidance a synchronous
  // preview/install 403 already gets from ApiErrorAlert.
  it("shows the PAT-scope guidance for a failed job with a git_credential_rejected error_code", async () => {
    server.use(
      http.get("*/api/v1/apps/jobs/:jobId", () =>
        HttpResponse.json({
          id: "job-1",
          app_id: "git.palmimo-teleop",
          kind: "update",
          state: "failed",
          step: "fetch",
          error: "git clone exited 128: fatal: unable to access: The requested URL returned error: 403",
          error_code: "git_credential_rejected",
          started_at: 0,
          finished_at: 1,
          lock_generated: false,
          dropped_bindings: [],
          dropped_params: [],
        }),
      ),
    );

    renderWithProviders(<AppJobDialog jobId="job-1" title="palmimo-teleop" onClose={vi.fn()} onDone={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(0));

    expect(await screen.findByText(/Git credentials were rejected/)).toBeInTheDocument();
  });
});

it.each(["firmware", "assets", "launch"])("renders localized start progress for %s", async (step) => {
  server.use(http.get("*/api/v1/apps/jobs/start-1", () => HttpResponse.json({ ...startJob, step })));
  renderWithProviders(<AppJobDialog jobId="start-1" title="Face" onClose={vi.fn()} onDone={vi.fn()} />);
  expect(await screen.findByRole("progressbar")).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getByRole("status")).not.toBeEmptyDOMElement();
    expect(screen.getByRole("status").textContent).not.toBe(step);
  });
});

it("shows a warning on a successful start", async () => {
  server.use(http.get("*/api/v1/apps/jobs/start-1", () => HttpResponse.json({ ...startJob, state: "done", warning: "absent" })));
  const done = vi.fn();
  renderWithProviders(<AppJobDialog jobId="start-1" title="Face" onClose={vi.fn()} onDone={done} />);
  expect(await screen.findByRole("alert")).not.toBeEmptyDOMElement();
  expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  expect(done).toHaveBeenCalledOnce();
});
