import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PortalApiError } from "@/api/client";
import { WifiConnectForm } from "@/components/WifiConnectForm";
import { renderWithProviders } from "@/test/render";

const SECURED_NETWORK = { ssid: "HomeNet", signal: 80, secured: true };
const OPEN_NETWORK = { ssid: "GuestNet", signal: 60, secured: false };

describe("WifiConnectForm", () => {
  it("disables submit and shows a hint for a 1-7 character password on a secured network", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={onSubmit}
        onBack={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText("Wi-Fi password"), "1234567");

    expect(screen.getByText("Password must be 8-63 characters.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeDisabled();

    // Submitting the form directly (e.g. pressing Enter) must not call
    // through either -- the disabled button alone isn't the only guard.
    await user.keyboard("{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("enables submit once the password reaches 8 characters", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={onSubmit}
        onBack={vi.fn()}
      />,
    );

    await user.type(screen.getByLabelText("Wi-Fi password"), "12345678");
    expect(screen.queryByText("Password must be 8-63 characters.")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();

    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(onSubmit).toHaveBeenCalledWith("12345678");
  });

  it("does not apply the short-password hint to an open network", async () => {
    const onSubmit = vi.fn();
    renderWithProviders(
      <WifiConnectForm
        network={OPEN_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={onSubmit}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
  });

  it("renders the wifi_invalid_ssid error message", () => {
    renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={new PortalApiError(400, "wifi_invalid_ssid", {})}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText("That network name is not valid.")).toBeInTheDocument();
  });

  it("toggles the password input between hidden and visible", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    const passwordInput = screen.getByLabelText("Wi-Fi password");
    expect(passwordInput).toHaveAttribute("type", "password");

    const toggle = screen.getByRole("button", { name: "Show or hide password" });
    await user.click(toggle);
    expect(passwordInput).toHaveAttribute("type", "text");
    expect(toggle).toHaveAttribute("aria-pressed", "true");

    await user.click(toggle);
    expect(passwordInput).toHaveAttribute("type", "password");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("never submits the form when the visibility toggle is clicked", async () => {
    // Uses the open network (no `required` password) so HTML5 constraint
    // validation can't mask a regression: an empty required field blocks
    // native submission on its own, which would hide a missing
    // `type="button"` on the toggle.
    const user = userEvent.setup();
    const { container } = renderWithProviders(
      <WifiConnectForm
        network={OPEN_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    const form = container.querySelector("form");
    if (!form) throw new Error("form not found in rendered output");
    const onFormSubmit = vi.fn();
    form.addEventListener("submit", onFormSubmit);

    await user.click(screen.getByRole("button", { name: "Show or hide password" }));

    expect(onFormSubmit).not.toHaveBeenCalled();
  });

  it("hides the password again once the target network changes", async () => {
    const user = userEvent.setup();
    const { rerender } = renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Show or hide password" }));
    expect(screen.getByLabelText("Wi-Fi password")).toHaveAttribute("type", "text");

    rerender(
      <WifiConnectForm
        network={OPEN_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByLabelText("Wi-Fi password")).toHaveAttribute("type", "password");
  });

  it("shows a connecting spinner and disables submit while isSubmitting", () => {
    renderWithProviders(
      <WifiConnectForm
        network={OPEN_NETWORK}
        lastAttempt={undefined}
        connectError={undefined}
        isSubmitting={true}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    const submitButton = screen.getByRole("button", { name: "Connecting…" });
    expect(submitButton).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
    expect(submitButton.querySelector(".animate-spin")).not.toBeNull();
  });

  it("renders the wifi_invalid_psk error message", () => {
    renderWithProviders(
      <WifiConnectForm
        network={SECURED_NETWORK}
        lastAttempt={undefined}
        connectError={new PortalApiError(400, "wifi_invalid_psk", {})}
        isSubmitting={false}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );

    expect(screen.getByText("That password is not valid. It must be 8-63 characters.")).toBeInTheDocument();
  });
});
