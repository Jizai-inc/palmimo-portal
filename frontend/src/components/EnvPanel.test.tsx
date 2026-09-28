import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { getListAppsApiV1AppsGetMockHandler } from "@/api/generated/apps/apps.msw";
import { getListGitCredentialsApiV1GitCredentialsGetMockHandler, getListSecretsApiV1SecretsGetMockHandler } from "@/api/generated/secrets/secrets.msw";
import { EnvPanel } from "@/components/EnvPanel";
import { GitCredentialsPanel } from "@/components/GitCredentialsPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

describe("EnvPanel", () => {
  it("registers a new secret with an uppercased name and its value", async () => {
    const user = userEvent.setup();
    let putBody: unknown;
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({ credentials: [] }),
      http.put("*/api/v1/secrets/WIFI_PASSWORD", async ({ request }) => {
        putBody = await request.json();
        return HttpResponse.json({ name: "WIFI_PASSWORD", updated_at: 1, used_by: [] });
      }),
    );
    renderWithProviders(<EnvPanel />);

    await screen.findByText("No environment variables registered yet.");
    await user.click(screen.getByRole("button", { name: "Register" }));
    await user.type(screen.getByLabelText("Name"), "wifi_password");
    await user.type(screen.getByLabelText("Value"), "hunter2");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putBody).toEqual({ value: "hunter2" }));
  });

  // remove it, and the operator would only discover the breakage the next time that app starts.
  it("shows the secret-in-use error inline when deleting a bound secret is refused", async () => {
    const user = userEvent.setup();
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [{ name: "WIFI_PASSWORD", updated_at: 1, used_by: [] }] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({ credentials: [] }),
      http.delete("*/api/v1/secrets/WIFI_PASSWORD", () =>
        HttpResponse.json({ error: { code: "secret_in_use", params: {} } }, { status: 409 }),
      ),
    );
    renderWithProviders(<EnvPanel />);

    await user.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("This secret is still bound to an app and cannot be deleted.")).toBeInTheDocument();
  });

  // instead of reading `used_by` straight off the list response.
  it("shows which apps use a secret, sourced from the list response's used_by field", async () => {
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({
        secrets: [{ name: "WIFI_PASSWORD", updated_at: 1, used_by: ["palmimo-teleop"] }],
      }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({ credentials: [] }),
    );
    renderWithProviders(<EnvPanel />);

    await screen.findByText("WIFI_PASSWORD");
    expect(await screen.findByText("palmimo-teleop")).toBeInTheDocument();
  });

  // The host/owner path segment (e.g. "github.com/Jizai-inc") itself contains a slash, and
  // `getPutGitCredentialApiV1GitCredentialsHostOwnerPutUrl` interpolates it unencoded -- so the
  // request lands on multiple path segments, not one; a mock (or route) matching only a single
  // `:hostOwner` segment would silently miss it.
  it("adds a git credential, keeping the host/owner's embedded slash in the request path", async () => {
    const user = userEvent.setup();
    let putHostOwner: string | undefined;
    let putBody: unknown;
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({ credentials: [] }),
      http.put("*/api/v1/git-credentials/*", async ({ request }) => {
        putHostOwner = new URL(request.url).pathname.replace(/^.*\/git-credentials\//, "");
        putBody = await request.json();
        return HttpResponse.json({ host_owner: putHostOwner, updated_at: 1 });
      }),
    );
    renderWithProviders(<GitCredentialsPanel />);

    await user.click(await screen.findByRole("button", { name: "Add credential" }));
    const dialog = screen.getByRole("alertdialog");
    await user.type(within(dialog).getByLabelText("Host / owner"), "github.com/Jizai-inc");
    await user.type(within(dialog).getByLabelText("Personal access token"), "ghp_abc");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(putHostOwner).toBe("github.com/Jizai-inc"));
    expect(putBody).toEqual({ value: "ghp_abc" });
  });

  it("marks a rejected git credential and leaves a non-rejected one unmarked", async () => {
    server.use(
      getListSecretsApiV1SecretsGetMockHandler({ secrets: [] }),
      getListAppsApiV1AppsGetMockHandler({ apps: [] }),
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({
        credentials: [
          { host_owner: "github.com/acme", updated_at: 1, rejected_at: 2 },
          { host_owner: "github.com/other", updated_at: 1, rejected_at: null },
        ],
      }),
    );
    renderWithProviders(<GitCredentialsPanel />);

    const acmeRow = (await screen.findByText("github.com/acme")).closest("li");
    const otherRow = (await screen.findByText("github.com/other")).closest("li");
    expect(acmeRow).not.toBeNull();
    expect(otherRow).not.toBeNull();
    expect(within(acmeRow as HTMLElement).getByText("Rejected")).toBeInTheDocument();
    expect(within(otherRow as HTMLElement).queryByText("Rejected")).not.toBeInTheDocument();
  });

  // installed apps depend on it, sourced straight off the list response's app_ids field.
  it("shows the apps installed from a credential's host/owner", async () => {
    server.use(
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({
        credentials: [{ host_owner: "github.com/jizai-inc", updated_at: 1, rejected_at: null, app_ids: ["palmimo.teleop"] }],
      }),
    );
    renderWithProviders(<GitCredentialsPanel />);

    const row = (await screen.findByText("github.com/jizai-inc")).closest("li");
    expect(within(row as HTMLElement).getByText("palmimo.teleop")).toBeInTheDocument();
  });

  it("shows replacement state for an existing host owner", async () => {
    const user = userEvent.setup();
    server.use(
      getListGitCredentialsApiV1GitCredentialsGetMockHandler({
        credentials: [{ host_owner: "github.com/jizai-inc", updated_at: 1, rejected_at: null }],
      }),
    );
    renderWithProviders(<GitCredentialsPanel />);

    await user.click(await screen.findByRole("button", { name: "Add credential" }));
    const dialog = screen.getByRole("alertdialog");
    await user.type(within(dialog).getByLabelText("Host / owner"), "github.com/Jizai-inc");
    expect(within(dialog).getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });
});
