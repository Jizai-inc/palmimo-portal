/// <reference types="node" />
//
// vitest's jsdom environment still runs inside a real Node.js process (jsdom only adds
// window/document on top), so node:child_process/fs/os/path are available here same as
// anywhere else -- no environment override needed. This test shells out to the real system
// `ssh-keygen` binary: the pure-function tests in sshKeygen.test.ts already pin every byte of
// the format, but only the actual OpenSSH implementation can confirm it accepts this output.
// The triple-slash reference (not tsconfig.app.json's "types") keeps Node's ambient globals out
// of the rest of this browser app's type-checking.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { formatOpenSshPrivateKey, formatOpenSshPublicKey } from "@/lib/sshKeygen";

// RFC 8032 section 7.1, test vector 1 -- the same vector sshKeygen.test.ts pins at the byte
// level; this test instead hands the pure functions' output to the real `ssh-keygen -y -f` and
// checks it derives exactly the expected public key from it.
const SEED_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const PUBLIC_KEY_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";

function hexToBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

function isSshKeygenOnPath(): boolean {
  const result = spawnSync("ssh-keygen", ["-h"]);
  return !(result.error && (result.error as NodeJS.ErrnoException).code === "ENOENT");
}

const sshKeygenAvailable = isSshKeygenOnPath();

describe("openssh-key-v1 output pinned against the system ssh-keygen", () => {
  it.skipIf(!sshKeygenAvailable)(
    "ssh-keygen -y -f derives exactly the expected public key from the generated private-key file" +
      (sshKeygenAvailable ? "" : " (skipped: ssh-keygen not found on PATH)"),
    () => {
      const seed = hexToBytes(SEED_HEX);
      const publicKey = hexToBytes(PUBLIC_KEY_HEX);
      const privateKeyFile = formatOpenSshPrivateKey(seed, publicKey, "palmimo-portal");
      const expectedPublicKeyLine = formatOpenSshPublicKey(publicKey, "palmimo-portal");

      // Written to a scratch tmpdir at test-run time and removed in `finally` -- never committed,
      // so this key material does not enter the tree the publication-hygiene guard
      // (tests/contracts/test_publication_hygiene.py) scans.
      const dir = mkdtempSync(join(tmpdir(), "palmimo-sshkeygen-test-"));
      const keyPath = join(dir, "palmimo_ed25519");
      try {
        writeFileSync(keyPath, privateKeyFile, { mode: 0o600 });
        const result = spawnSync("ssh-keygen", ["-y", "-f", keyPath], { encoding: "utf-8" });
        expect(result.status).toBe(0);
        expect(result.stdout.trim()).toBe(expectedPublicKeyLine);
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    },
  );
});
