import { screen } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/** Opens the Radix Select named `name` and returns its options in display order. */
export async function openSelect(user: UserEvent, name: string): Promise<HTMLElement[]> {
  await user.click(screen.getByRole("combobox", { name }));
  return screen.findAllByRole("option");
}

/** Index of the selected option; the trigger shows only the label, which can repeat across options. */
export async function selectedOptionIndex(user: UserEvent, name: string): Promise<number> {
  const options = await openSelect(user, name);
  const index = options.findIndex((option) => option.dataset.state === "checked");
  await user.keyboard("{Escape}");
  return index;
}

export async function chooseOption(user: UserEvent, name: string, option: string | number): Promise<void> {
  const options = await openSelect(user, name);
  await user.click(typeof option === "number" ? options[option] : await screen.findByRole("option", { name: option }));
}
