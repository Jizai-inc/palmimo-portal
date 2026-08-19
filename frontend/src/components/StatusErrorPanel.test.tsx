import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { StatusErrorPanel } from "@/components/StatusErrorPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function stubStatus({ deviceId }: { deviceId: string | null }) {
  server.use(
    http.get("*/api/v1/system/status", () =>
      HttpResponse.json({
        state: "unprovisioned",
        hostname: "palmimo-1234",
        auth_state: "corrupt",
        device_id: deviceId,
        versions: { portal: "0.1.0", sdk: null },
        last_wifi_attempt: null,
        adapters: "fake",
        state_dir: "/tmp",
      }),
    ),
  );
}

describe("StatusErrorPanel", () => {
  it("shows the reset button and reset-mentioning copy on the corrupt screen when device_id is present (identity device)", async () => {
    stubStatus({ deviceId: "1234" });
    renderWithProviders(<StatusErrorPanel reason="corrupt" onRetry={vi.fn()} onReset={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Reset login" })).toBeInTheDocument();
    await screen.findByText(
      "This device's saved credentials could not be read. Delete auth.json over SSH, or reset the login below.",
    );
  });

  it("hides the reset button and any mention of it on the corrupt screen when device_id is absent (DIY device)", async () => {
    stubStatus({ deviceId: null });
    renderWithProviders(<StatusErrorPanel reason="corrupt" onRetry={vi.fn()} onReset={vi.fn()} />);

    await screen.findByText("This device's saved credentials could not be read. Delete auth.json over SSH to recover.");
    expect(screen.queryByText(/reset/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset login" })).not.toBeInTheDocument();
  });

  it("calls onReset when the reset button is clicked", async () => {
    stubStatus({ deviceId: "1234" });
    const user = userEvent.setup();
    const onReset = vi.fn();
    renderWithProviders(<StatusErrorPanel reason="corrupt" onRetry={vi.fn()} onReset={onReset} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));

    expect(onReset).toHaveBeenCalledTimes(1);
  });

  it("shows a retry button, never a reset button, on the unavailable screen", async () => {
    stubStatus({ deviceId: "1234" });
    renderWithProviders(<StatusErrorPanel reason="unavailable" onRetry={vi.fn()} onReset={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reset login" })).not.toBeInTheDocument();
  });

  it("calls onRetry and disables the button while retrying", async () => {
    stubStatus({ deviceId: "1234" });
    const user = userEvent.setup();
    let resolveRetry: () => void = () => {};
    const onRetry = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRetry = resolve;
        }),
    );
    renderWithProviders(<StatusErrorPanel reason="unavailable" onRetry={onRetry} onReset={vi.fn()} />);

    const button = await screen.findByRole("button", { name: "Retry" });
    await user.click(button);

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();

    resolveRetry();
  });
});
