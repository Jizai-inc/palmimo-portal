/**
 * Pure encoding helpers for client-side Ed25519 SSH key generation.
 *
 * WebCrypto (`crypto.subtle.generateKey`/`exportKey`) produces the raw key
 * material; everything here is byte-format plumbing with no crypto calls of
 * its own, so it is unit-testable against fixed RFC 8032 vectors without a
 * WebCrypto implementation.
 */

const KEY_TYPE = "ssh-ed25519";
const CIPHER_BLOCK_SIZE = 8;

/** Feature-detect Ed25519 support (Chrome 113+/Safari 17+); older browsers lack the algorithm entirely. */
export function supportsEd25519Keygen(): boolean {
  return typeof crypto !== "undefined" && typeof crypto.subtle?.generateKey === "function";
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
 * Ed25519 PKCS#8 (RFC 5958 / RFC 8410) is a fixed 48-byte DER structure for
 * this algorithm; the 32-byte private seed is always its last 32 bytes
 * (verified against the RFC 8032 test-1 vector in sshKeygen.test.ts).
 */
export function extractEd25519SeedFromPkcs8(pkcs8Bytes: Uint8Array): Uint8Array {
  if (pkcs8Bytes.length !== 48) {
    throw new Error(`expected a 48-byte Ed25519 PKCS#8 structure, got ${pkcs8Bytes.length} bytes`);
  }
  return pkcs8Bytes.slice(16);
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
 * The only function here that touches WebCrypto: generates an Ed25519 key
 * pair with `crypto.subtle`, entirely client-side, and formats both halves.
 * The private key material (`seed`) never leaves this call -- it is used
 * only to build `privateKeyFile`'s string, never returned, logged, or sent
 * anywhere.
 */
export async function generateEd25519KeyPair(comment: string): Promise<GeneratedEd25519KeyPair> {
  const keyPair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  const publicKeyBytes = new Uint8Array(await crypto.subtle.exportKey("raw", keyPair.publicKey));
  const pkcs8Bytes = new Uint8Array(await crypto.subtle.exportKey("pkcs8", keyPair.privateKey));
  const seed = extractEd25519SeedFromPkcs8(pkcs8Bytes);

  return {
    publicKeyLine: formatOpenSshPublicKey(publicKeyBytes, comment),
    privateKeyFile: formatOpenSshPrivateKey(seed, publicKeyBytes, comment),
  };
}
