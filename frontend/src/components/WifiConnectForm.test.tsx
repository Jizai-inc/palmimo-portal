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
