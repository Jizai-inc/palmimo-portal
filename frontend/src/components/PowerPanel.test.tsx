import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { UpdateStatusResponse } from "@/api/generated/models";
import { getRebootApiV1SystemRebootPostMockHandler } from "@/api/generated/system/system.msw";
import { getGetStatusApiV1UpdateStatusGetMockHandler } from "@/api/generated/update/update.msw";
import { PowerPanel } from "@/components/PowerPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function jsonError(status: number, code: string, params: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, params } }, { status });
}

const IDLE_UPDATE_STATUS: UpdateStatusResponse = {
  installed: { tag: "v1.0.0", commit: "abc1234" },
  latest: null,
  checked_at: null,
  update_available: false,
  previous_tag: null,
  retry_available: false,
  job: { kind: "update", state: "idle", target: null, step: null, error: null, started_at: null, finished_at: null, restarting_at: null },
};

describe("PowerPanel", () => {
  // PowerPanel polls update/status to disable its buttons during an update;
  // every test needs a default handler, overridden where a test exercises
  // that behavior directly.
  beforeEach(() => {
    server.use(getGetStatusApiV1UpdateStatusGetMockHandler(IDLE_UPDATE_STATUS));
  });

  it("reboots: dialog -> confirm -> POST called -> rebooting state shown", async () => {
    const user = userEvent.setup();
    let rebootCalled = false;
    server.use(
      http.post("*/api/v1/system/reboot", async () => {
        rebootCalled = true;
        await delay(20);
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    expect(await screen.findByText("Restart Palmimo?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).not.toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Restart" }));

    // While the mutation is pending, Cancel must not be clickable -- the
    // user cannot dismiss the dialog mid-flight and lose the outcome.
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());

    await waitFor(() => expect(rebootCalled).toBe(true));
    expect(await screen.findByText("Restarting…")).toBeInTheDocument();
  });

  it("calls onRebooted once a status poll fails and then succeeds again", async () => {
    const user = userEvent.setup();
    const onRebooted = vi.fn();
    let statusCallCount = 0;
    server.use(
      getRebootApiV1SystemRebootPostMockHandler({ status: "ok" }),
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
          versions: { portal: "0.1.0", sdk: null },
          last_wifi_attempt: null,
          adapters: "fake",
          state_dir: "/tmp",
        });
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={onRebooted} pollIntervalMs={20} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Restart" }));

    await screen.findByText("Restarting…");
    await waitFor(() => expect(statusCallCount).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(onRebooted).toHaveBeenCalledTimes(1));
  });

  it("counts a hung status poll (no response, ever) as a failure via the per-request timeout", async () => {
    const user = userEvent.setup();
    const onRebooted = vi.fn();
    let statusCallCount = 0;
    server.use(
      getRebootApiV1SystemRebootPostMockHandler({ status: "ok" }),
      http.get("*/api/v1/system/status", async () => {
        statusCallCount += 1;
        if (statusCallCount === 1) {
          // Simulate a host mid-reboot that never answers the SYN: this
          // handler never resolves. Without AbortSignal.timeout on the
          // request, this poll would hang forever and `hasFailed` would
          // never flip -- onRebooted would never fire.
          await delay("infinite");
        }
        return HttpResponse.json({
          state: "connected",
          hostname: "palmimo-1234",
          auth_state: "set",
          device_id: "1234",
          versions: { portal: "0.1.0", sdk: null },
          last_wifi_attempt: null,
          adapters: "fake",
          state_dir: "/tmp",
        });
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={onRebooted} pollIntervalMs={30} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Restart" }));

    await screen.findByText("Restarting…");
    await waitFor(() => expect(onRebooted).toHaveBeenCalledTimes(1));
    expect(onRebooted).toHaveBeenCalledTimes(1);
  });

  it("keeps the has-failed-once flag across a re-render that swaps onRebooted's identity", async () => {
    const user = userEvent.setup();
    const onRebootedOld = vi.fn();
    const onRebootedNew = vi.fn();
    let statusCallCount = 0;
    let allowSuccess = false;
    server.use(
      getRebootApiV1SystemRebootPostMockHandler({ status: "ok" }),
      http.get("*/api/v1/system/status", () => {
        statusCallCount += 1;
        if (!allowSuccess) {
          return HttpResponse.error();
        }
        return HttpResponse.json({
          state: "connected",
          hostname: "palmimo-1234",
          auth_state: "set",
          device_id: "1234",
          versions: { portal: "0.1.0", sdk: null },
          last_wifi_attempt: null,
          adapters: "fake",
          state_dir: "/tmp",
        });
      }),
    );
    const { rerender } = renderWithProviders(<PowerPanel onRebooted={onRebootedOld} pollIntervalMs={20} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Restart" }));

    await screen.findByText("Restarting…");
    // At least one poll has failed by now (every poll fails until
    // `allowSuccess` flips below).
    await waitFor(() => expect(statusCallCount).toBeGreaterThanOrEqual(1));

    // A brand-new inline callback identity, exactly as a route re-render
    // would produce -- this must not reset the "has failed once" state.
    rerender(<PowerPanel onRebooted={onRebootedNew} pollIntervalMs={20} />);
    allowSuccess = true;

    await waitFor(() => expect(onRebootedNew).toHaveBeenCalledTimes(1));
    expect(onRebootedOld).not.toHaveBeenCalled();
  });

  it("shuts down: dialog -> confirm -> POST called -> shutting-down state", async () => {
    const user = userEvent.setup();
    let shutdownCalled = false;
    server.use(
      http.post("*/api/v1/system/shutdown", () => {
        shutdownCalled = true;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    await user.click(screen.getByRole("button", { name: "Shut down" }));
    expect(await screen.findByText("Shut down Palmimo?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Shut down" }));

    await waitFor(() => expect(shutdownCalled).toBe(true));
    expect(await screen.findByText("Palmimo is shutting down")).toBeInTheDocument();
  });

  it("shows an error alert and re-enables the buttons on a 503", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/system/reboot", () => jsonError(503, "system_backend_unavailable")));
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Restart" }));

    expect(await screen.findByText("Palmimo could not complete that system action. Try again shortly.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Shut down" })).not.toBeDisabled();
  });

  it("issues no request when the dialog is cancelled", async () => {
    const user = userEvent.setup();
    let rebootCalled = false;
    server.use(
      http.post("*/api/v1/system/reboot", () => {
        rebootCalled = true;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    await user.click(screen.getByRole("button", { name: "Restart" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(rebootCalled).toBe(false);
  });

  it("disables both buttons and shows a note while an update is running", async () => {
    server.use(
      getGetStatusApiV1UpdateStatusGetMockHandler({
        ...IDLE_UPDATE_STATUS,
        job: { kind: "update", state: "running", target: "v2.0.0", step: "sync", error: null, started_at: 1, finished_at: null, restarting_at: null },
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    expect(await screen.findByText("An update is in progress. Restart and shut down are disabled until it finishes.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Shut down" })).toBeDisabled();
  });

  it("does not disable the buttons once the update job has finished", async () => {
    server.use(
      getGetStatusApiV1UpdateStatusGetMockHandler({
        ...IDLE_UPDATE_STATUS,
        job: { kind: "update", state: "done", target: "v2.0.0", step: null, error: null, started_at: 1, finished_at: 2, restarting_at: null },
      }),
    );
    renderWithProviders(<PowerPanel onRebooted={vi.fn()} pollIntervalMs={50} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Restart" })).not.toBeDisabled());
    expect(screen.queryByText("An update is in progress. Restart and shut down are disabled until it finishes.")).not.toBeInTheDocument();
  });
});
