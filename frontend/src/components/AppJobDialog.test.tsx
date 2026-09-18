import { act, screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppJobDialog } from "@/components/AppJobDialog";
import { renderWithProviders } from "@/test/render";
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
});
