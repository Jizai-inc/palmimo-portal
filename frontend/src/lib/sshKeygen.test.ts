import { getPublicKey as nobleGetPublicKey } from "@noble/ed25519";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  encodeEd25519PublicKeyBlob,
  formatOpenSshPrivateKey,
  formatOpenSshPublicKey,
  probeEd25519KeygenSupport,
  resetEd25519KeygenSupportProbeForTests,
  supportsEd25519Keygen,
} from "@/lib/sshKeygen";

// RFC 8032 section 7.1, test vector 1.
const SEED_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60";
const PUBLIC_KEY_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";

function hexToBytes(hex: string): Uint8Array {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return bytes;
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

const SEED = hexToBytes(SEED_HEX);
const PUBLIC_KEY = hexToBytes(PUBLIC_KEY_HEX);

describe("noble Ed25519 derivation (importing sshKeygen.ts wires @noble/hashes' sha512 in)", () => {
  it("derives the RFC 8032 test-1 public key from its seed", () => {
    expect(bytesToHex(nobleGetPublicKey(SEED))).toBe(PUBLIC_KEY_HEX);
  });
});

describe("formatOpenSshPublicKey", () => {
  it("encodes the RFC 8032 test-1 key as the known ssh-ed25519 public-key line", () => {
    const line = formatOpenSshPublicKey(PUBLIC_KEY, "palmimo-portal");

    expect(line).toBe(
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINdamAGCsQq31Uv+08lkBzoO4XLz2qYjJa8CGmj3B1Ea palmimo-portal",
    );
  });

  it("prefixes the blob with the ssh-ed25519 type string", () => {
    const blob = encodeEd25519PublicKeyBlob(PUBLIC_KEY);
    // 4-byte length prefix (11) + "ssh-ed25519"
    expect(blob.slice(0, 4)).toEqual(new Uint8Array([0, 0, 0, 11]));
    expect(new TextDecoder().decode(blob.slice(4, 15))).toBe("ssh-ed25519");
    // Then the 4-byte length prefix (32) + the raw public key.
    expect(blob.slice(15, 19)).toEqual(new Uint8Array([0, 0, 0, 32]));
    expect(blob.slice(19)).toEqual(PUBLIC_KEY);
  });
});

describe("formatOpenSshPrivateKey", () => {
  const privateKeyPem = formatOpenSshPrivateKey(SEED, PUBLIC_KEY, "palmimo-portal");
  const bodyBase64 = privateKeyPem
    .split("\n")
    .filter((line) => line && !line.startsWith("-----"))
    .join("");
  const body = Uint8Array.from(atob(bodyBase64), (c) => c.charCodeAt(0));

  it("starts and ends with the OpenSSH PEM armor", () => {
    expect(privateKeyPem.startsWith("-----BEGIN OPENSSH PRIVATE KEY-----\n")).toBe(true);
    expect(privateKeyPem.trimEnd().endsWith("-----END OPENSSH PRIVATE KEY-----")).toBe(true);
  });

  it("starts with the openssh-key-v1 magic and null terminator", () => {
    const magic = new TextDecoder().decode(body.slice(0, 15));
    expect(magic).toBe("openssh-key-v1\0");
  });

  it("declares ciphername and kdfname \"none\" and a single key", () => {
    let offset = 15;
    function readString(): Uint8Array {
      const len = new DataView(body.buffer, body.byteOffset + offset, 4).getUint32(0, false);
      offset += 4;
      const value = body.slice(offset, offset + len);
      offset += len;
      return value;
    }
    expect(new TextDecoder().decode(readString())).toBe("none"); // ciphername
    expect(new TextDecoder().decode(readString())).toBe("none"); // kdfname
    expect(readString()).toEqual(new Uint8Array(0)); // kdfoptions
    const numKeys = new DataView(body.buffer, body.byteOffset + offset, 4).getUint32(0, false);
    offset += 4;
    expect(numKeys).toBe(1);
  });

  it("embeds the same public key blob in the header and inside the private section", () => {
    let offset = 15;
    function readString(): Uint8Array {
      const len = new DataView(body.buffer, body.byteOffset + offset, 4).getUint32(0, false);
      offset += 4;
      const value = body.slice(offset, offset + len);
      offset += len;
      return value;
    }
    readString(); // ciphername
    readString(); // kdfname
    readString(); // kdfoptions
    offset += 4; // numKeys
    const headerPublicKeyBlob = readString();
    const privateSection = readString();

    expect(headerPublicKeyBlob).toEqual(encodeEd25519PublicKeyBlob(PUBLIC_KEY));

    let privOffset = 8; // skip the two 4-byte checkints
    function readPrivString(): Uint8Array {
      const len = new DataView(privateSection.buffer, privateSection.byteOffset + privOffset, 4).getUint32(0, false);
      privOffset += 4;
      const value = privateSection.slice(privOffset, privOffset + len);
      privOffset += len;
      return value;
    }
    const checkint1 = privateSection.slice(0, 4);
    const checkint2 = privateSection.slice(4, 8);
    expect(checkint1).toEqual(checkint2);

    expect(new TextDecoder().decode(readPrivString())).toBe("ssh-ed25519");
    expect(readPrivString()).toEqual(PUBLIC_KEY);
    const seedAndPublic = readPrivString();
    expect(seedAndPublic.slice(0, 32)).toEqual(SEED);
    expect(seedAndPublic.slice(32)).toEqual(PUBLIC_KEY);
    expect(new TextDecoder().decode(readPrivString())).toBe("palmimo-portal");

    const padding = privateSection.slice(privOffset);
    for (let i = 0; i < padding.length; i++) expect(padding[i]).toBe(i + 1);
  });

  it("pads the private section to a multiple of the 8-byte cipher block size", () => {
    let offset = 15;
    function readString(): Uint8Array {
      const len = new DataView(body.buffer, body.byteOffset + offset, 4).getUint32(0, false);
      offset += 4;
      const value = body.slice(offset, offset + len);
      offset += len;
      return value;
    }
    readString(); // ciphername
    readString(); // kdfname
    readString(); // kdfoptions
    offset += 4; // numKeys
    readString(); // public key blob
    const privateSection = readString();
    expect(privateSection.length % 8).toBe(0);
  });
});

describe("supportsEd25519Keygen", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns true when crypto.getRandomValues exists", () => {
    expect(supportsEd25519Keygen()).toBe(true);
  });

  it("returns false when crypto.getRandomValues is unavailable", () => {
    // crypto.subtle is what's actually missing in the portal's real (insecure-context) target
    // environment -- this simulates the more extreme case of no usable `crypto` at all.
    vi.stubGlobal("crypto", {});
    expect(supportsEd25519Keygen()).toBe(false);
  });
});

describe("probeEd25519KeygenSupport", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    resetEd25519KeygenSupportProbeForTests();
  });

  it("resolves true for a real, unmocked generation (this runs the actual noble/hashes code path)", async () => {
    await expect(probeEd25519KeygenSupport()).resolves.toBe(true);
  });

  it("resolves false when crypto.getRandomValues is unavailable, without attempting a derivation", async () => {
    vi.stubGlobal("crypto", {});
    await expect(probeEd25519KeygenSupport()).resolves.toBe(false);
  });

  it("resolves false, rather than throwing, when getRandomValues itself throws", async () => {
    vi.stubGlobal("crypto", {
      getRandomValues: () => {
        throw new Error("boom");
      },
    });
    await expect(probeEd25519KeygenSupport()).resolves.toBe(false);
  });

  it("memoizes the result: a later stub change does not affect an already-resolved probe", async () => {
    await expect(probeEd25519KeygenSupport()).resolves.toBe(true);
    vi.stubGlobal("crypto", {});
    await expect(probeEd25519KeygenSupport()).resolves.toBe(true);
  });
});
