/**
 * Pure encoding helpers, plus Ed25519 key generation, for client-side SSH
 * key generation.
 *
 * Generation deliberately does NOT use WebCrypto (`crypto.subtle`):
 * `SubtleCrypto` is available only in a secure context (HTTPS, or
 * localhost), and the Palmimo Portal is served over plain HTTP on the
 * device's LAN by design (see doc/design/ in the palmimo-portal repo for
 * why). On the real device `crypto.subtle` is `undefined`, so a
 * `crypto.subtle`-based implementation silently has no generate button at
 * all -- this was caught on-device, not in tests, because Node/vitest's
 * WebCrypto has no secure-context gate. `@noble/ed25519` (wired to
 * `@noble/hashes`'s pure-JS sha512, so it never touches `crypto.subtle`
 * either) plus `crypto.getRandomValues` -- which, unlike `crypto.subtle`,
 * IS available in insecure contexts -- works everywhere the portal runs.
 * Do not "simplify" this back to `crypto.subtle.generateKey`.
 *
 * Everything below the generation functions is pure byte-format plumbing
 * with no crypto calls of its own, so it is unit-testable against fixed
 * RFC 8032 vectors independent of either crypto backend.
 */

import { getPublicKey as nobleGetPublicKey, hashes as nobleHashes } from "@noble/ed25519";
import { sha512 } from "@noble/hashes/sha2.js";

// One-time wiring so noble's synchronous API (no crypto.subtle involved, unlike its default
// async methods -- see the module doc comment above) has a hash function to use at all.
nobleHashes.sha512 = sha512;

const KEY_TYPE = "ssh-ed25519";
const CIPHER_BLOCK_SIZE = 8;

/** Cheap existence check; see {@link probeEd25519KeygenSupport} for a real capability probe. */
export function supportsEd25519Keygen(): boolean {
  return typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function";
}

let cachedSupportProbe: Promise<boolean> | null = null;

/**
 * Decides button visibility by actually attempting a generation, not just
 * checking `crypto.getRandomValues` exists -- cheap insurance against a
 * runtime surprise in noble's derivation on some engine. Memoized for the
 * page's lifetime; the answer cannot change without a reload.
 */
export function probeEd25519KeygenSupport(): Promise<boolean> {
  if (!cachedSupportProbe) {
    cachedSupportProbe = (async () => {
      if (!supportsEd25519Keygen()) return false;
      try {
        const probeSeed = new Uint8Array(32);
        crypto.getRandomValues(probeSeed);
        nobleGetPublicKey(probeSeed);
        return true;
      } catch {
        return false;
      }
    })();
  }
  return cachedSupportProbe;
}

/** Test-only: clears the memoized probe result so a test can force it to re-run. */
export function resetEd25519KeygenSupportProbeForTests(): void {
  cachedSupportProbe = null;
}

function utf8Bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

/** SSH wire format: a 4-byte big-endian length prefix followed by the bytes. */
function encodeSshString(bytes: Uint8Array): Uint8Array {
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length, false);
  out.set(bytes, 4);
  return out;
}

function concatBytes(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** The SSH wire-format encoding of an Ed25519 public key: `string("ssh-ed25519") || string(raw 32-byte key)`. */
export function encodeEd25519PublicKeyBlob(publicKeyBytes: Uint8Array): Uint8Array {
  return concatBytes([encodeSshString(utf8Bytes(KEY_TYPE)), encodeSshString(publicKeyBytes)]);
}

/** The one-line `ssh-ed25519 AAAA... comment` public key format written to `authorized_keys`/`.pub` files. */
export function formatOpenSshPublicKey(publicKeyBytes: Uint8Array, comment: string): string {
  const base64Blob = bytesToBase64(encodeEd25519PublicKeyBlob(publicKeyBytes));
  return `${KEY_TYPE} ${base64Blob} ${comment}`;
}

/**
 * The `openssh-key-v1` unencrypted private-key file (as written by
 * `ssh-keygen`/OpenSSH for an Ed25519 key), built directly from the raw
 * 32-byte seed and 32-byte public key -- see PROTOCOL.key in the OpenSSH
 * source for the format this follows.
 */
export function formatOpenSshPrivateKey(seed: Uint8Array, publicKeyBytes: Uint8Array, comment: string): string {
  if (seed.length !== 32) throw new Error(`expected a 32-byte Ed25519 seed, got ${seed.length} bytes`);
  if (publicKeyBytes.length !== 32) throw new Error(`expected a 32-byte Ed25519 public key, got ${publicKeyBytes.length} bytes`);

  const publicKeyBlob = encodeEd25519PublicKeyBlob(publicKeyBytes);

  // checkint1/checkint2: two identical random 32-bit values OpenSSH uses to
  // verify the (would-be) decryption succeeded. Unencrypted, any fixed value
  // works as long as both match; use crypto.getRandomValues for parity with
  // real `ssh-keygen` output.
  const checkint = new Uint32Array(1);
  crypto.getRandomValues(checkint);
  const checkintBytes = new Uint8Array(4);
  new DataView(checkintBytes.buffer).setUint32(0, checkint[0], false);

  // Private-key section: checkint || checkint || string(keytype) ||
  // string(pubkey) || string(seed || pubkey) || string(comment) || padding.
  const privateKeyBlobBeforePadding = concatBytes([
    checkintBytes,
    checkintBytes,
    encodeSshString(utf8Bytes(KEY_TYPE)),
    encodeSshString(publicKeyBytes),
    encodeSshString(concatBytes([seed, publicKeyBytes])),
    encodeSshString(utf8Bytes(comment)),
  ]);

  // Pad with the byte sequence 1, 2, 3, ... up to a multiple of the cipher
  // block size (8 for "none"), as required by openssh-key-v1.
  const paddingLength = (CIPHER_BLOCK_SIZE - (privateKeyBlobBeforePadding.length % CIPHER_BLOCK_SIZE)) % CIPHER_BLOCK_SIZE;
  const padding = new Uint8Array(paddingLength);
  for (let i = 0; i < paddingLength; i++) padding[i] = i + 1;
  const privateKeyBlob = concatBytes([privateKeyBlobBeforePadding, padding]);

  const magic = concatBytes([utf8Bytes("openssh-key-v1"), new Uint8Array([0])]);
  const body = concatBytes([
    magic,
    encodeSshString(utf8Bytes("none")), // ciphername
    encodeSshString(utf8Bytes("none")), // kdfname
    encodeSshString(new Uint8Array(0)), // kdfoptions (empty)
    (() => {
      const numKeys = new Uint8Array(4);
      new DataView(numKeys.buffer).setUint32(0, 1, false);
      return numKeys;
    })(),
    encodeSshString(publicKeyBlob),
    encodeSshString(privateKeyBlob),
  ]);

  const base64Body = bytesToBase64(body);
  const wrapped = base64Body.match(/.{1,70}/g) ?? [];
  return ["-----BEGIN OPENSSH PRIVATE KEY-----", ...wrapped, "-----END OPENSSH PRIVATE KEY-----", ""].join("\n");
}

export interface GeneratedEd25519KeyPair {
  publicKeyLine: string;
  privateKeyFile: string;
}

/**
 * The only function here that generates key material, entirely
 * client-side: a random 32-byte seed from `crypto.getRandomValues`, its
 * Ed25519 public key via `@noble/ed25519` (see the module doc comment for
 * why not WebCrypto), then both halves formatted. The seed never leaves
 * this call -- it is used only to build `privateKeyFile`'s string, never
 * returned on its own, logged, or sent anywhere.
 */
export async function generateEd25519KeyPair(comment: string): Promise<GeneratedEd25519KeyPair> {
  const seed = new Uint8Array(32);
  crypto.getRandomValues(seed);
  const publicKeyBytes = nobleGetPublicKey(seed);

  return {
    publicKeyLine: formatOpenSshPublicKey(publicKeyBytes, comment),
    privateKeyFile: formatOpenSshPrivateKey(seed, publicKeyBytes, comment),
  };
}
