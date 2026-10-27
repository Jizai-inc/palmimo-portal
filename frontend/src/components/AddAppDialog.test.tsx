import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import { AddAppDialog } from "@/components/AddAppDialog";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const PREVIEW = {
  name: "my-app",
  namespace: "alice",
  suggested_name: "my-app",
  suggested_id: "alice.my-app",
  description: "An app.",
  devices: [],
  env: [],
};

function stubPreviewAndInstall() {
  server.use(
    getListAppsApiV1AppsGetMockHandler({ apps: [] }),
    getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
    http.post("*/api/v1/apps/preview", () => HttpResponse.json(PREVIEW)),
    http.post("*/api/v1/apps/install", () =>
      HttpResponse.json(
        { job: { id: "job-1", app_id: null, kind: "install", state: "running", step: "fetch", error: null, started_at: 1, finished_at: null, dropped_bindings: [], dropped_params: [], lock_generated: false } },
        { status: 202 },
      ),
    ),
    http.get("*/api/v1/apps/jobs/job-1", () =>
      HttpResponse.json({ id: "job-1", app_id: "alice.my-app", kind: "install", state: "done", step: "register", error: null, started_at: 1, finished_at: 2, dropped_bindings: [], dropped_params: [], lock_generated: false }),
    ),
  );
}

async function fillGithubForm(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Repository URL"), "https://github.com/alice/my-app");
  await user.type(screen.getByLabelText("Branch or tag"), "main");
}

describe("AddAppDialog", () => {
  it("shows the confirmation in place of the form, and Back returns to the filled-in form", async () => {
    const user = userEvent.setup();
    stubPreviewAndInstall();
    renderWithProviders(<AddAppDialog open onOpenChange={() => undefined} onInstalled={() => undefined} />);

    await fillGithubForm(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));

    expect(await screen.findByText("my-app")).toBeInTheDocument();
    expect(screen.queryByLabelText("Repository URL")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Zip" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("alertdialog")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Back" }));

    expect(screen.getByLabelText("Repository URL")).toHaveValue("https://github.com/alice/my-app");
  });

  it("closes on install and hands the job to the progress dialog, reporting the installed app id", async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onInstalled = vi.fn();
    stubPreviewAndInstall();
    renderWithProviders(<AddAppDialog open onOpenChange={onOpenChange} onInstalled={onInstalled} />);

    await fillGithubForm(user);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await screen.findByText("my-app");
    await user.click(screen.getByRole("button", { name: "Install" }));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith("alice.my-app"));
  });

  it("opens on the zip source when asked", async () => {
    stubPreviewAndInstall();
    renderWithProviders(<AddAppDialog open initialTab="zip" onOpenChange={() => undefined} onInstalled={() => undefined} />);

    const dialog = within(await screen.findByRole("alertdialog"));
    expect(dialog.getByRole("button", { name: "Zip" })).toHaveAttribute("aria-current", "page");
  });
});
