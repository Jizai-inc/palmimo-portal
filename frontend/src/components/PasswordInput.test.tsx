import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PasswordInput } from "@/components/PasswordInput";
import { renderWithProviders } from "@/test/render";

describe("PasswordInput", () => {
  it("toggles the input between hidden and visible", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PasswordInput aria-label="Password" />);

    const input = screen.getByLabelText("Password");
    expect(input).toHaveAttribute("type", "password");

    const toggle = screen.getByRole("button", { name: "Show or hide password" });
    await user.click(toggle);
    expect(input).toHaveAttribute("type", "text");
    expect(toggle).toHaveAttribute("aria-pressed", "true");

    await user.click(toggle);
    expect(input).toHaveAttribute("type", "password");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps autocapitalize, autocorrect, and spellcheck off once shown as text", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PasswordInput aria-label="Password" />);

    await user.click(screen.getByRole("button", { name: "Show or hide password" }));

    const input = screen.getByLabelText("Password");
    expect(input).toHaveAttribute("autocapitalize", "none");
    expect(input).toHaveAttribute("autocorrect", "off");
    expect(input).toHaveAttribute("spellcheck", "false");
  });

  it("never submits the enclosing form when the toggle is clicked", async () => {
    // No `required` on the input: an empty required field blocks native
    // submission on its own, which would hide a missing `type="button"`.
    const user = userEvent.setup();
    const onSubmit = vi.fn((event: React.FormEvent) => event.preventDefault());
    renderWithProviders(
      <form onSubmit={onSubmit}>
        <PasswordInput aria-label="Password" />
      </form>,
    );

    await user.click(screen.getByRole("button", { name: "Show or hide password" }));

    expect(onSubmit).not.toHaveBeenCalled();
  });
});
