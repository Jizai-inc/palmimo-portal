import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { LoginPanel } from "@/components/LoginPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

function stubStatus(overrides: Partial<{ auth_state: string; device_id: string | null; state: string }> = {}) {
  server.use(
    http.get("*/api/v1/system/status", () =>
      HttpResponse.json({
        state: "unprovisioned",
        hostname: "palmimo-1234",
        auth_state: "set",
        device_id: null,
        versions: { portal: "0.1.0", sdk: null },
        last_wifi_attempt: null,
        adapters: "fake",
        state_dir: "/tmp",
        ...overrides,
      }),
    ),
  );
}

describe("LoginPanel", () => {
  it("shows the forgot-password link on the normal login screen when device_id is present", async () => {
    stubStatus({ auth_state: "set", device_id: "1234" });
    renderWithProviders(<LoginPanel onLoggedIn={vi.fn()} onForgotPassword={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Forgot your password?" })).toBeInTheDocument();
  });

  it("hides the forgot-password link when device_id is absent (DIY device)", async () => {
    stubStatus({ auth_state: "set", device_id: null });
    renderWithProviders(<LoginPanel onLoggedIn={vi.fn()} onForgotPassword={vi.fn()} />);

    await screen.findByLabelText("Password");
    expect(screen.queryByRole("button", { name: "Forgot your password?" })).not.toBeInTheDocument();
  });

  it("hides the forgot-password link on the sticker (initial) login screen even with device_id present", async () => {
    stubStatus({ auth_state: "initial", device_id: "1234" });
    renderWithProviders(<LoginPanel onLoggedIn={vi.fn()} onForgotPassword={vi.fn()} />);

    await screen.findByText("1234");
    expect(screen.queryByRole("button", { name: "Forgot your password?" })).not.toBeInTheDocument();
  });

  it("calls onForgotPassword when the link is clicked", async () => {
    stubStatus({ auth_state: "set", device_id: "1234" });
    const user = userEvent.setup();
    const onForgotPassword = vi.fn();
    renderWithProviders(<LoginPanel onLoggedIn={vi.fn()} onForgotPassword={onForgotPassword} />);

    await user.click(await screen.findByRole("button", { name: "Forgot your password?" }));

    expect(onForgotPassword).toHaveBeenCalledTimes(1);
  });

  it("submits the password and calls onLoggedIn with mode full and connected", async () => {
    stubStatus({ auth_state: "set", device_id: "1234", state: "connected" });
    const user = userEvent.setup();
    const onLoggedIn = vi.fn();
    server.use(http.post("*/api/v1/auth/login", () => HttpResponse.json({ status: "ok", mode: "full" })));
    renderWithProviders(<LoginPanel onLoggedIn={onLoggedIn} onForgotPassword={vi.fn()} />);

    await user.type(await screen.findByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Log in" }));

    await waitFor(() => expect(onLoggedIn).toHaveBeenCalledWith("full", true));
  });

  it("invalidates the system/status query on a successful login", async () => {
    stubStatus({ auth_state: "set", device_id: "1234", state: "connected" });
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/auth/login", () => HttpResponse.json({ status: "ok", mode: "full" })));
    const { queryClient } = renderWithProviders(<LoginPanel onLoggedIn={vi.fn()} onForgotPassword={vi.fn()} />);
    await screen.findByLabelText("Password"); // wait for the initial system/status fetch to settle
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");

    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Log in" }));

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["/api/v1/system/status"] })),
    );
  });

  it("calls onLoggedIn with mode initial after a sticker-password login", async () => {
    stubStatus({ auth_state: "initial", device_id: "1234" });
    const user = userEvent.setup();
    const onLoggedIn = vi.fn();
    server.use(http.post("*/api/v1/auth/login", () => HttpResponse.json({ status: "ok", mode: "initial" })));
    renderWithProviders(<LoginPanel onLoggedIn={onLoggedIn} onForgotPassword={vi.fn()} />);

    await user.type(await screen.findByLabelText("Password"), "sticker-pw");
    await user.click(screen.getByRole("button", { name: "Log in" }));

    await waitFor(() => expect(onLoggedIn).toHaveBeenCalledWith("initial", false));
  });
});
