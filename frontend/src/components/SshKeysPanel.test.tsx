import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, delay, http } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getListKeysApiV1SshKeysGetMockHandler } from "@/api/generated/ssh-keys/ssh-keys.msw";
import {
  getGetStatusApiV1SystemStatusGetMockHandler,
  getGetStatusApiV1SystemStatusGetResponseMock,
} from "@/api/generated/system/system.msw";
import type { SshKeyResponse } from "@/api/generated/models";
import { SshKeysPanel } from "@/components/SshKeysPanel";
import * as sshKeygen from "@/lib/sshKeygen";
import { renderWithProviders } from "@/test/render";
import { server } from "@/test/server";

// The generate-key flow is unit-tested against known vectors in sshKeygen.test.ts; here it is
// mocked so the component tests exercise the UI wiring deterministically (a real generation
// yields a different key each call) without risking a global crypto stub breaking MSW's own
// use of it.
vi.mock("@/lib/sshKeygen", async (importOriginal) => ({
  ...(await importOriginal<typeof sshKeygen>()),
  probeEd25519KeygenSupport: vi.fn(() => Promise.resolve(true)),
}));

// jsdom does not implement URL.createObjectURL/revokeObjectURL at all, so these are added (not
// replaced) directly on the real URL constructor for the whole file -- stubbing the whole global
// would also break MSW's own use of `new URL(...)` to match requests.
URL.createObjectURL = vi.fn(() => "blob:mock-url");
URL.revokeObjectURL = vi.fn();

// The two branch buttons carry their help text inside their accessible name, so they are matched
// by prefix rather than by an exact string.
const GENERATE_CHOICE = /^Create a key in this browser/;
const REGISTER_CHOICE = /^Register a key you already have/;

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

async function chooseRegisterBranch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: REGISTER_CHOICE }));
}

async function chooseGenerateBranch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: GENERATE_CHOICE }));
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
    expect(screen.queryByRole("button", { name: REGISTER_CHOICE })).not.toBeInTheDocument();
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
    await chooseRegisterBranch(user);
    await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");
    await user.click(screen.getByRole("button", { name: "Add key" }));

    await waitFor(() => expect(postedBody).toEqual({ public_key: "ssh-ed25519 AAAAtest user@laptop" }));
    expect(await screen.findByText("ssh-ed25519")).toBeInTheDocument();
    expect(listCallCount).toBeGreaterThanOrEqual(2);
    expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();
  });

  it("shows the translated error for an invalid key format", async () => {
    const user = userEvent.setup();
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      http.post("*/api/v1/ssh-keys", () => jsonError(400, "invalid_key_format")),
    );
    renderWithProviders(<SshKeysPanel />);

    await screen.findByText("No keys registered yet.");
    await chooseRegisterBranch(user);
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
    await chooseRegisterBranch(user);
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

  it("names the generated private key file in the ready-to-copy ssh command once the hostname loads", async () => {
    server.use(
      getListKeysApiV1SshKeysGetMockHandler([]),
      getGetStatusApiV1SystemStatusGetMockHandler(getGetStatusApiV1SystemStatusGetResponseMock({ hostname: "palmimo-406" })),
    );
    renderWithProviders(<SshKeysPanel />);

    expect(await screen.findByText("ssh -i ~/.ssh/palmimo_ed25519 user@palmimo-406.local")).toBeInTheDocument();
    expect(screen.getByText("Using a key of your own? Replace the file name after -i.")).toBeInTheDocument();
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

    await screen.findByText("ssh -i ~/.ssh/palmimo_ed25519 user@palmimo-406.local");
    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledWith("ssh -i ~/.ssh/palmimo_ed25519 user@palmimo-406.local");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();

    vi.unstubAllGlobals();
  });

  describe("choosing how to add a key", () => {
    it("offers both branches and shows neither one's fields until a branch is picked", async () => {
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      expect(await screen.findByRole("button", { name: GENERATE_CHOICE })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: REGISTER_CHOICE })).toBeInTheDocument();
      expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Name (comment)")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Generate key" })).not.toBeInTheDocument();
    });

    it("shows no generate UI in the register branch", async () => {
      const user = userEvent.setup();
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);

      expect(screen.getByLabelText("Public key")).toBeInTheDocument();
      expect(screen.getByLabelText("Name (comment)")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Generate key" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: GENERATE_CHOICE })).not.toBeInTheDocument();
    });

    it("returns to the choice when the branch is backed out of", async () => {
      const user = userEvent.setup();
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");
      await user.click(screen.getByRole("button", { name: "Back" }));

      expect(await screen.findByRole("button", { name: GENERATE_CHOICE })).toBeInTheDocument();
      expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();

      await chooseRegisterBranch(user);
      expect(screen.getByLabelText("Public key")).toHaveValue("");
    });

    it("goes straight to the register branch, with no choice to make, when the browser cannot generate keys", async () => {
      vi.mocked(sshKeygen.probeEd25519KeygenSupport).mockResolvedValue(false);
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      expect(await screen.findByLabelText("Public key")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: GENERATE_CHOICE })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: REGISTER_CHOICE })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();

      vi.mocked(sshKeygen.probeEd25519KeygenSupport).mockResolvedValue(true);
    });
  });

  describe("naming a key in the register branch", () => {
    it("fills the name from the pasted key's own comment", async () => {
      const user = userEvent.setup();
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");

      expect(screen.getByLabelText("Name (comment)")).toHaveValue("user@laptop");
    });

    it("posts the key under the entered name instead of the pasted one", async () => {
      const user = userEvent.setup();
      let postedBody: unknown;
      server.use(
        getListKeysApiV1SshKeysGetMockHandler([]),
        http.post("*/api/v1/ssh-keys", async ({ request }) => {
          postedBody = await request.json();
          return HttpResponse.json(ONE_KEY[0], { status: 201 });
        }),
      );
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");
      await user.clear(screen.getByLabelText("Name (comment)"));
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Add key" }));

      await waitFor(() => expect(postedBody).toEqual({ public_key: "ssh-ed25519 AAAAtest keisuke@macbook" }));
    });

    it("drops the pasted comment when the name is cleared", async () => {
      const user = userEvent.setup();
      let postedBody: unknown;
      server.use(
        getListKeysApiV1SshKeysGetMockHandler([]),
        http.post("*/api/v1/ssh-keys", async ({ request }) => {
          postedBody = await request.json();
          return HttpResponse.json(ONE_KEY[0], { status: 201 });
        }),
      );
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest user@laptop");
      await user.clear(screen.getByLabelText("Name (comment)"));
      await user.click(screen.getByRole("button", { name: "Add key" }));

      await waitFor(() => expect(postedBody).toEqual({ public_key: "ssh-ed25519 AAAAtest" }));
    });

    it("posts a multi-line paste as it stands, rather than folding its extra keys into a comment", async () => {
      const user = userEvent.setup();
      const twoKeys = "ssh-ed25519 AAAAone one@laptop\nssh-ed25519 AAAAtwo two@laptop";
      let postedBody: unknown;
      server.use(
        getListKeysApiV1SshKeysGetMockHandler([]),
        http.post("*/api/v1/ssh-keys", async ({ request }) => {
          postedBody = await request.json();
          return jsonError(400, "invalid_key_format");
        }),
      );
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), twoKeys);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Add key" }));

      await waitFor(() => expect(postedBody).toEqual({ public_key: twoKeys }));
      expect(await screen.findByText("That does not look like a valid public key.")).toBeInTheDocument();
    });

    it("keeps the pasted line as typed when no name is given", async () => {
      const user = userEvent.setup();
      let postedBody: unknown;
      server.use(
        getListKeysApiV1SshKeysGetMockHandler([]),
        http.post("*/api/v1/ssh-keys", async ({ request }) => {
          postedBody = await request.json();
          return HttpResponse.json(ONE_KEY[0], { status: 201 });
        }),
      );
      renderWithProviders(<SshKeysPanel />);

      await chooseRegisterBranch(user);
      await user.type(screen.getByLabelText("Public key"), "ssh-ed25519 AAAAtest");
      await user.click(screen.getByRole("button", { name: "Add key" }));

      await waitFor(() => expect(postedBody).toEqual({ public_key: "ssh-ed25519 AAAAtest" }));
    });
  });

  describe("browser key generation", () => {
    const publicKeyLine =
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea keisuke@macbook";
    const privateKeyFile = "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----\n";

    afterEach(() => {
      vi.mocked(sshKeygen.probeEd25519KeygenSupport).mockResolvedValue(true);
      vi.restoreAllMocks();
    });

    it("hides the generate branch when the browser has no real Ed25519 keygen support", async () => {
      vi.mocked(sshKeygen.probeEd25519KeygenSupport).mockResolvedValue(false);
      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await screen.findByText("No keys registered yet.");
      await waitFor(() => expect(vi.mocked(sshKeygen.probeEd25519KeygenSupport)).toHaveBeenCalled());
      expect(screen.queryByRole("button", { name: GENERATE_CHOICE })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Generate key" })).not.toBeInTheDocument();
    });

    it("cannot generate until the key is named, and names the key pair after that entry", async () => {
      const user = userEvent.setup();
      const generate = vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      expect(screen.getByRole("button", { name: "Generate key" })).toBeDisabled();

      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));

      expect(generate).toHaveBeenCalledWith("keisuke@macbook");
      expect(screen.getByLabelText("Name (comment)")).toBeDisabled();
    });

    it("surfaces an error instead of failing silently when generation itself throws", async () => {
      const user = userEvent.setup();
      vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockRejectedValue(new Error("boom"));

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));

      expect(await screen.findByText(/Key generation failed/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "I saved the private key" })).not.toBeInTheDocument();
      expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();
    });

    it("blocks registering the public key until the private key is explicitly confirmed saved", async () => {
      const user = userEvent.setup();
      vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));

      // Auto-download was attempted (best-effort), but that alone must not register anything:
      // there is no public key field on screen yet, so there is nothing to submit.
      expect(clickSpy).toHaveBeenCalledTimes(1);
      expect(screen.queryByLabelText("Public key")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Add key" })).not.toBeInTheDocument();
      expect(screen.getByText("A lost private key cannot be recovered. Generate a new one if you lose it")).toBeInTheDocument();
      expect(screen.getByText(/mv ~\/Downloads\/palmimo_ed25519 ~\/\.ssh\//)).toBeInTheDocument();
      expect(screen.getByText(/chmod 600 ~\/\.ssh\/palmimo_ed25519/)).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Generate key" })).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "I saved the private key" }));

      expect(screen.getByLabelText("Public key")).toHaveValue(publicKeyLine);
      expect(screen.getByRole("button", { name: "Add key" })).toBeEnabled();
      expect(screen.queryByText("A lost private key cannot be recovered. Generate a new one if you lose it")).not.toBeInTheDocument();
      expect(screen.getByText(/matching public key has been filled in below/)).toBeInTheDocument();
    });

    it("withholds the way out only while an unconfirmed private key is on screen", async () => {
      const user = userEvent.setup();
      vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();

      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));

      // Leaving here would strand a private key the user has not decided about yet.
      expect(screen.queryByRole("button", { name: "Back" })).not.toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "I saved the private key" }));

      // Starting over is the only escape left for a download that never actually landed.
      expect(screen.getByRole("button", { name: "Back" })).toBeInTheDocument();
    });

    it("locks the name once generation starts, so the key cannot be renamed mid-flight", async () => {
      const user = userEvent.setup();
      let finishGeneration: (pair: { publicKeyLine: string; privateKeyFile: string }) => void = () => {};
      vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockReturnValue(
        new Promise((resolve) => {
          finishGeneration = resolve;
        }),
      );
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));

      expect(screen.getByLabelText("Name (comment)")).toBeDisabled();

      finishGeneration({ publicKeyLine, privateKeyFile });
      expect(await screen.findByRole("button", { name: "I saved the private key" })).toBeInTheDocument();
    });

    it("mints a fresh download URL per press, so a retry works after the first has been revoked", async () => {
      const user = userEvent.setup();
      const createObjectURL = vi.mocked(URL.createObjectURL);
      createObjectURL.mockClear();
      const generate = vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));
      await user.click(screen.getByRole("button", { name: "Download again" }));

      expect(createObjectURL).toHaveBeenCalledTimes(2);
      expect(generate).toHaveBeenCalledTimes(1);
    });

    it("re-downloading reuses the same private-key file without generating a new key", async () => {
      const user = userEvent.setup();
      const generate = vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      server.use(getListKeysApiV1SshKeysGetMockHandler([]));
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));
      await user.click(screen.getByRole("button", { name: "Download again" }));
      await user.click(screen.getByRole("button", { name: "Download again" }));

      expect(generate).toHaveBeenCalledTimes(1);
      expect(clickSpy).toHaveBeenCalledTimes(3); // 1 automatic + 2 manual re-downloads
    });

    it("returns to the branch choice after the generated public key is successfully added", async () => {
      const user = userEvent.setup();
      vi.spyOn(sshKeygen, "generateEd25519KeyPair").mockResolvedValue({ publicKeyLine, privateKeyFile });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

      let listCallCount = 0;
      server.use(
        http.get("*/api/v1/ssh-keys", () => {
          listCallCount += 1;
          return HttpResponse.json(listCallCount === 1 ? [] : ONE_KEY);
        }),
        http.post("*/api/v1/ssh-keys", () => HttpResponse.json(ONE_KEY[0], { status: 201 })),
      );
      renderWithProviders(<SshKeysPanel />);

      await chooseGenerateBranch(user);
      await user.type(screen.getByLabelText("Name (comment)"), "keisuke@macbook");
      await user.click(screen.getByRole("button", { name: "Generate key" }));
      await user.click(screen.getByRole("button", { name: "I saved the private key" }));
      await user.click(screen.getByRole("button", { name: "Add key" }));

      await waitFor(() =>
        expect(screen.queryByText(/matching public key has been filled in below/)).not.toBeInTheDocument(),
      );
      expect(await screen.findByRole("button", { name: GENERATE_CHOICE })).toBeInTheDocument();
    });
  });
});
