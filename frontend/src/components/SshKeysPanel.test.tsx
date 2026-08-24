import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import { getListKeysApiV1SshKeysGetMockHandler } from "@/api/generated/ssh-keys/ssh-keys.msw";
import {
  getGetStatusApiV1SystemStatusGetMockHandler,
  getGetStatusApiV1SystemStatusGetResponseMock,
} from "@/api/generated/system/system.msw";
import type { SshKeyResponse } from "@/api/generated/models";
import { SshKeysPanel } from "@/components/SshKeysPanel";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

const ONE_KEY: SshKeyResponse[] = [
  { fingerprint: "SHA256:aaaa1111bbbb2222", key_type: "ssh-ed25519", comment: "user@laptop" },
];

const TWO_KEYS: SshKeyResponse[] = [
  { fingerprint: "SHA256:aaaa1111bbbb2222", key_type: "ssh-ed25519", comment: "user@laptop" },
  { fingerprint: "SHA256:cccc3333dddd4444", key_type: "ssh-rsa", comment: "" },
];

function jsonError(status: number, code: string, params: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, params } }, { status });
}

describe("SshKeysPanel", () => {
  it("renders each key's type, comment, and fingerprint", async () => {
    server.use(getListKeysApiV1SshKeysGetMockHandler(TWO_KEYS));
    renderWithProviders(<SshKeysPanel />);

    expect(await screen.findByText("ssh-ed25519")).toBeInTheDocument();
    expect(screen.getByText("user@laptop")).toBeInTheDocument();
    expect(screen.getByText("SHA256:aaaa1111bbbb2222")).toBeInTheDocument();
    expect(screen.getByText("ssh-rsa")).toBeInTheDocument();
    expect(screen.getByText("SHA256:cccc3333dddd4444")).toBeInTheDocument();
  });

  it("truncates a long comment instead of pushing the delete button out of the row", async () => {
    const longComment = "a".repeat(200);
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([
        { fingerprint: "SHA256:aaaa1111bbbb2222", key_type: "ssh-ed25519", comment: longComment },
      ]),
    );
    renderWithProviders(<SshKeysPanel />);

    const comment = await screen.findByText(longComment);
    const classes = comment.className.split(" ");
    expect(classes).toContain("min-w-0");
    // Unprefixed, not `md:truncate` -- the row is `flex-col` (not `md:grid`) below md, so
    // truncation must apply at mobile widths too, not just once the desktop grid kicks in.
    expect(classes).toContain("truncate");
  });

  it("shows an empty state when there are no keys", async () => {
    server.use(getListKeysApiV1SshKeysGetMockHandler([]));
    renderWithProviders(<SshKeysPanel />);

    expect(await screen.findByText("No keys registered yet.")).toBeInTheDocument();
  });

  it("shows only the translated list error, and neither the empty state nor the add-key form, when the list fails", async () => {
    server.use(http.get("*/api/v1/ssh-keys", () => jsonError(503, "network_backend_unavailable")));
    renderWithProviders(<SshKeysPanel />);

    expect(await screen.findByText("Palmimo could not reach its Wi-Fi service. Try again shortly.")).toBeInTheDocument();
    expect(screen.queryByText("No keys registered yet.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add key" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();
  });

  it("posts the pasted key text and refreshes the list on success", async () => {
    const user = userEvent.setup();
    let listCallCount = 0;
    let postedBody: unknown;
    server.use(
      http.get("*/api/v1/ssh-keys", () => {
        listCallCount += 1;
        return HttpResponse.json(listCallCount === 1 ? [] : ONE_KEY);
      }),
      http.post("*/api/v1/ssh-keys", async ({ request }) => {
        postedBody = await request.json();
        return HttpResponse.json(ONE_KEY[0], { status: 201 });
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("No keys registered yet.");
    await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");
    await user.click(screen.getByRole("button", { name: "Add key" }));

    await waitFor(() => expect(postedBody).toEqual({ public_key: "ssh-ed25519 AAAAtest user@laptop" }));
    expect(await screen.findByText("ssh-ed25519")).toBeInTheDocument();
    expect(listCallCount).toBeGreaterThanOrEqual(2);
    expect(screen.getByLabelText("Public key")).toHaveValue("");
  });

  it("shows the translated error for an invalid key format", async () => {
    const user = userEvent.setup();
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      http.post("*/api/v1/ssh-keys", () => jsonError(400, "invalid_key_format")),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("No keys registered yet.");
    await user.type(screen.getByLabelText("Public key"), "not a key");
    await user.click(screen.getByRole("button", { name: "Add key" }));

    expect(await screen.findByText("That does not look like a valid public key.")).toBeInTheDocument();
  });

  it("deletes a non-last key without a confirm param after confirming the dialog", async () => {
    const user = userEvent.setup();
    let deleteUrl: string | undefined;
    server.use(
      getListKeysApiV1SshKeysGetMockHandler(TWO_KEYS),
      http.delete("*/api/v1/ssh-keys/:fingerprint", ({ request }) => {
        deleteUrl = request.url;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh-ed25519");
    const rows = screen.getAllByRole("button", { name: "Delete" });
    await user.click(rows[0]);

    expect(await screen.findByText("Delete this key?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteUrl).toBeDefined());
    expect(deleteUrl).toContain("/api/v1/ssh-keys/SHA256:aaaa1111bbbb2222");
    expect(new URL(deleteUrl!).searchParams.has("confirm")).toBe(false);
  });

  it("deletes the last key with confirm=last-key and shows the lockout warning first", async () => {
    const user = userEvent.setup();
    let deleteUrl: string | undefined;
    server.use(
      getListKeysApiV1SshKeysGetMockHandler(ONE_KEY),
      http.delete("*/api/v1/ssh-keys/:fingerprint", ({ request }) => {
        deleteUrl = request.url;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh-ed25519");
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("Delete the last key?")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete anyway" }));

    await waitFor(() => expect(deleteUrl).toBeDefined());
    expect(new URL(deleteUrl!).searchParams.get("confirm")).toBe("last-key");
  });

  it("re-opens the dialog in last-key mode when the server answers 409 mid-flight, then confirms with confirm=last-key", async () => {
    const user = userEvent.setup();
    let deleteCallCount = 0;
    let lastDeleteUrl: string | undefined;
    server.use(
      getListKeysApiV1SshKeysGetMockHandler(TWO_KEYS),
      http.delete("*/api/v1/ssh-keys/:fingerprint", ({ request }) => {
        deleteCallCount += 1;
        lastDeleteUrl = request.url;
        if (deleteCallCount === 1) {
          return jsonError(409, "last_key_deletion_requires_confirmation");
        }
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh-ed25519");
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    await user.click(deleteButtons[0]);

    expect(await screen.findByText("Delete this key?")).toBeInTheDocument();
    let dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    // The server's 409 reopens the dialog in last-key mode instead of just
    // rendering an inline error.
    expect(await screen.findByText("Delete the last key?")).toBeInTheDocument();
    dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete anyway" }));

    await waitFor(() => expect(deleteCallCount).toBe(2));
    expect(new URL(lastDeleteUrl!).searchParams.get("confirm")).toBe("last-key");
  });

  it("clears a stale 409 error when the reopened last-key dialog is cancelled", async () => {
    const user = userEvent.setup();
    server.use(
      getListKeysApiV1SshKeysGetMockHandler(TWO_KEYS),
      http.delete("*/api/v1/ssh-keys/:fingerprint", () => jsonError(409, "last_key_deletion_requires_confirmation")),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh-ed25519");
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    await user.click(deleteButtons[0]);

    expect(await screen.findByText("Delete this key?")).toBeInTheDocument();
    let dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    // The 409 reopens the dialog in last-key mode.
    expect(await screen.findByText("Delete the last key?")).toBeInTheDocument();
    dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(
      screen.queryByText(
        "This is the last key on file. Palmimo will no longer accept SSH logins until a new key is added from this page — your Portal login itself is unaffected.",
      ),
    ).not.toBeInTheDocument();
  });

  it("fills the public-key textarea with the trimmed contents of a chosen .pub file", async () => {
    const user = userEvent.setup();
    server.use(getListKeysApiV1SshKeysGetMockHandler([]));
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("No keys registered yet.");
    const file = new File(["ssh-ed25519 AAAAtest user@laptop\n"], "id_ed25519.pub", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Choose a .pub file"), file);

    expect(screen.getByLabelText("Public key")).toHaveValue("ssh-ed25519 AAAAtest user@laptop");
  });

  it("disables Cancel while a delete is pending", async () => {
    const user = userEvent.setup();
    server.use(
      getListKeysApiV1SshKeysGetMockHandler(TWO_KEYS),
      http.delete("*/api/v1/ssh-keys/:fingerprint", async () => {
        await delay(20);
        return HttpResponse.json({ status: "ok" });
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh-ed25519");
    const deleteButtons = screen.getAllByRole("button", { name: "Delete" });
    await user.click(deleteButtons[0]);

    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).not.toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());
  });

  it("renders the ready-to-copy ssh command once the hostname loads", async () => {
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      getGetStatusApiV1SystemStatusGetMockHandler(getGetStatusApiV1SystemStatusGetResponseMock({ hostname: "palmimo-406" })),
    );
    renderWithProviders(<SshKeysPanel />);

    expect(await screen.findByText("ssh user@palmimo-406.local")).toBeInTheDocument();
  });

  it("renders no ssh command while the hostname has not loaded yet", async () => {
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      http.get("*/api/v1/system/status", async () => {
        await delay(20);
        return HttpResponse.json({});
      }),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("No keys registered yet.");
    expect(screen.queryByText("Connect over SSH:", { exact: false })).not.toBeInTheDocument();
  });

  it("copies the ssh command to the clipboard when the copy button is clicked", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      getGetStatusApiV1SystemStatusGetMockHandler(getGetStatusApiV1SystemStatusGetResponseMock({ hostname: "palmimo-406" })),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("ssh user@palmimo-406.local");
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith("ssh user@palmimo-406.local");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

    vi.unstubAllGlobals();
  });
});
