import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { useGetStatusApiV1SystemStatusGet } from "@/api/generated/system/system";
import { DisplaySyncBanner } from "@/components/DisplaySyncBanner";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function StatusBanner() {
  const { data } = useGetStatusApiV1SystemStatusGet();
  return <DisplaySyncBanner status={data?.display_sync} />;
}

describe("DisplaySyncBanner", () => {
  it.each([[false, false], [true, false], [false, true], [true, true]])("shows pending=%s running=%s", (pending, running) => {
    renderWithProviders(<DisplaySyncBanner status={{ pending, running, reason: null }} />);
    expect(screen.queryByRole("status") !== null).toBe(pending || running);
    expect(screen.queryByRole("button") !== null).toBe(pending);
    if (pending && running) expect(screen.getByRole("button")).toBeDisabled();
  });

  it("waits for manual sync and preserves a busy result", async () => {
    let release!: () => void;
    let releaseRefresh!: () => void;
    let statusReads = 0;
    let refreshing = false;
    const called = vi.fn();
    server.use(http.post("*/api/v1/display-assets/sync", async () => {
      called();
      await new Promise<void>((resolve) => { release = resolve; });
      return HttpResponse.json({ result: "busy", reason: "in use", put: [], deleted: [] });
    }));
    server.use(http.get("*/api/v1/system/status", async () => {
      statusReads += 1;
      if (statusReads > 1) {
        refreshing = true;
        await new Promise<void>((resolve) => { releaseRefresh = resolve; });
      }
      return HttpResponse.json({ display_sync: { pending: true, running: false, reason: null } });
    }));
    const { queryClient } = renderWithProviders(<StatusBanner />);
    await screen.findByRole("button");
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const before = screen.getByRole("status").textContent;
    await userEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(called).toHaveBeenCalledOnce());
    expect(screen.getByRole("button")).toBeDisabled();
    release();
    await waitFor(() => expect(refreshing).toBe(true));
    expect(screen.getByRole("button")).toBeDisabled();
    releaseRefresh();
    await waitFor(() => expect(screen.getByRole("button")).toBeEnabled());
    expect(screen.getByRole("status").textContent).not.toBe(before);
    expect(invalidate).toHaveBeenCalled();
  });
});

it.each(["result", "http"])("clears a %s failure after pending work is resolved", async (failure) => {
  server.use(http.post("*/api/v1/display-assets/sync", () => failure === "result"
    ? HttpResponse.json({ result: "failed", reason: "unavailable", put: [], deleted: [] })
    : HttpResponse.json({ error: { code: "display_sync_failed", params: {} } }, { status: 500 })));
  const { rerender } = renderWithProviders(<DisplaySyncBanner status={{ pending: true, running: false, reason: null }} />);
  await userEvent.click(screen.getByRole("button"));
  expect(await screen.findByRole("alert")).not.toBeEmptyDOMElement();
  rerender(<DisplaySyncBanner status={{ pending: false, running: false, reason: null }} />);
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  rerender(<DisplaySyncBanner status={{ pending: true, running: false, reason: null }} />);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
