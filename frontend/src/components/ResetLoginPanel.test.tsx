import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { ResetLoginPanel } from "@/components/ResetLoginPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function jsonError(status: number, code: string, params: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, params } }, { status });
}

function stubStatus(hostname = "palmimo-1234") {
  server.use(
    http.get("*/api/v1/system/status", () =>
      HttpResponse.json({
        state: "connected",
        hostname,
        auth_state: "set",
        device_id: "1234",
        versions: { portal: "0.1.0", sdk: null },
        last_wifi_attempt: null,
        adapters: "fake",
        state_dir: "/tmp",
      }),
    ),
  );
}

describe("ResetLoginPanel", () => {
  it("renders the three points and the target device's hostname", async () => {
    stubStatus();
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    expect(await screen.findByText("palmimo-1234")).toBeInTheDocument();
    expect(screen.getByText("Wi-Fi settings and SSH keys are kept")).toBeInTheDocument();
    expect(screen.getByText("After resetting, you'll need the password on the device's sticker")).toBeInTheDocument();
    expect(
      screen.getByText("Anyone on the same network can do this (it only resets — it cannot be used to take over the device)"),
    ).toBeInTheDocument();
  });

  it("calls onBack when Back is clicked", async () => {
    stubStatus();
    const user = userEvent.setup();
    const onBack = vi.fn();
    renderWithProviders(<ResetLoginPanel onBack={onBack} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Back" }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("dialog -> confirm -> POST /auth/reset called -> success state -> onDone", async () => {
    stubStatus();
    const user = userEvent.setup();
    const onDone = vi.fn();
    let resetCalled = false;
    server.use(
      http.post("*/api/v1/auth/reset", () => {
        resetCalled = true;
        return HttpResponse.json({ status: "ok", auth_state: "initial" });
      }),
    );
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={onDone} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    expect(await screen.findByText("Reset your login?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    await waitFor(() => expect(resetCalled).toBe(true));
    expect(await screen.findByText("Reset complete")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Go to login" }));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it("clears the query cache once the reset succeeds", async () => {
    stubStatus();
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/reset", () => HttpResponse.json({ status: "ok", auth_state: "initial" })));
    const { queryClient } = renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);
    await screen.findByText("palmimo-1234"); // wait for the initial system/status fetch to settle
    const clearSpy = vi.spyOn(queryClient, "clear");

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    await waitFor(() => expect(clearSpy).toHaveBeenCalledTimes(1));
  });

  it("issues no request when the dialog is cancelled", async () => {
    stubStatus();
    const user = userEvent.setup();
    let resetCalled = false;
    server.use(
      http.post("*/api/v1/auth/reset", () => {
        resetCalled = true;
        return HttpResponse.json({ status: "ok", auth_state: "initial" });
      }),
    );
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(resetCalled).toBe(false);
  });

  it("shows the translated rate-limit error with seconds on 429", async () => {
    stubStatus();
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/reset", () => jsonError(429, "auth_rate_limited", { retry_after_seconds: 42 })));
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    expect(await screen.findByText("Too many attempts. Try again in 42 seconds.")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.queryByText("Reset complete")).not.toBeInTheDocument();
  });

  it("shows the translated error on 403 reset_not_available", async () => {
    stubStatus();
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/reset", () => jsonError(403, "reset_not_available")));
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    expect(
      await screen.findByText("This device cannot be reset from here. Delete auth.json over SSH."),
    ).toBeInTheDocument();
  });

  it("shows the translated error on 409 auth_not_set", async () => {
    stubStatus();
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/reset", () => jsonError(409, "auth_not_set")));
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    expect(await screen.findByText("No password has been set for this device yet.")).toBeInTheDocument();
  });

  it("shows the translated error on 503 identity_unavailable", async () => {
    stubStatus();
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/reset", () => jsonError(503, "identity_unavailable")));
    renderWithProviders(<ResetLoginPanel onBack={vi.fn()} onDone={vi.fn()} />);

    await user.click(await screen.findByRole("button", { name: "Reset login" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Reset" }));

    expect(
      await screen.findByText("This device's identity could not be read yet. Try again shortly."),
    ).toBeInTheDocument();
  });
});
