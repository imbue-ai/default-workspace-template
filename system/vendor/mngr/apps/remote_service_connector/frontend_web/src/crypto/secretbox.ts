// Envelope encryption, wire-compatible with imbue_common.secret_wrapping:
// each account has a random 32-byte DEK; secrets are AES-256-GCM encrypted
// directly under it as nonce||ciphertext blobs; the DEK itself travels only
// wrapped (same AEAD shape) under a KEK derived from the master password via
// argon2id. Parameter shapes and the bundle JSON match the Python side
// exactly (see scripts/generate_crypto_vectors.py + the vitest vectors).

import { argon2id } from "hash-wasm";

export const KEY_LENGTH_BYTES = 32;
export const KDF_SALT_LENGTH_BYTES = 16;
const AESGCM_NONCE_LENGTH_BYTES = 12;

// argon2id parameters following the RFC 9106 low-memory recommendation,
// matching imbue_common.secret_wrapping's defaults. Stored alongside every
// wrapped DEK so they can be raised later without breaking existing bundles.
export const DEFAULT_KDF_TIME_COST = 3;
export const DEFAULT_KDF_MEMORY_KIB = 65536;
export const DEFAULT_KDF_PARALLELISM = 4;

export class WrongPasswordOrCorruptDataError extends Error {}
export class MalformedCiphertextError extends Error {}

export interface KdfParameters {
  saltB64: string;
  timeCost: number;
  memoryKib: number;
  parallelism: number;
}

// The wire form of the per-account password-wrapped data key (the connector's
// AccountKeyBundleModel).
export interface KeyBundle {
  kdf_salt: string;
  kdf_time_cost: number;
  kdf_memory_kib: number;
  kdf_parallelism: number;
  wrapped_dek: string;
  key_epoch: number;
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export function base64ToBytes(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function randomBytes(length: number): Uint8Array {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return bytes;
}

export function generateDek(): Uint8Array {
  return randomBytes(KEY_LENGTH_BYTES);
}

export function generateKdfParameters(): KdfParameters {
  return {
    saltB64: bytesToBase64(randomBytes(KDF_SALT_LENGTH_BYTES)),
    timeCost: DEFAULT_KDF_TIME_COST,
    memoryKib: DEFAULT_KDF_MEMORY_KIB,
    parallelism: DEFAULT_KDF_PARALLELISM,
  };
}

// Derive the 32-byte KEK from the master password. Deterministic for a given
// (password, parameters) pair; an empty password is a valid input.
export async function deriveKek(
  password: string,
  parameters: KdfParameters,
): Promise<Uint8Array> {
  const rawKey = await argon2id({
    password,
    salt: base64ToBytes(parameters.saltB64),
    iterations: parameters.timeCost,
    memorySize: parameters.memoryKib,
    parallelism: parameters.parallelism,
    hashLength: KEY_LENGTH_BYTES,
    outputType: "binary",
  });
  return rawKey as Uint8Array;
}

async function importAesKey(key: Uint8Array): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    key as BufferSource,
    { name: "AES-GCM" },
    false,
    ["encrypt", "decrypt"],
  );
}

// AEAD-encrypt as nonce||ciphertext (WebCrypto appends the GCM tag to the
// ciphertext, matching Python's AESGCM.encrypt output).
export async function aeadEncrypt(
  key: Uint8Array,
  plaintext: Uint8Array,
): Promise<Uint8Array> {
  const nonce = randomBytes(AESGCM_NONCE_LENGTH_BYTES);
  const aesKey = await importAesKey(key);
  const ciphertext = new Uint8Array(
    await crypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce as BufferSource },
      aesKey,
      plaintext as BufferSource,
    ),
  );
  const blob = new Uint8Array(nonce.length + ciphertext.length);
  blob.set(nonce, 0);
  blob.set(ciphertext, nonce.length);
  return blob;
}

export async function aeadDecrypt(
  key: Uint8Array,
  blob: Uint8Array,
): Promise<Uint8Array> {
  if (blob.length <= AESGCM_NONCE_LENGTH_BYTES) {
    throw new MalformedCiphertextError(
      `Ciphertext blob is too short (${blob.length} bytes) to contain a nonce`,
    );
  }
  const nonce = blob.slice(0, AESGCM_NONCE_LENGTH_BYTES);
  const ciphertext = blob.slice(AESGCM_NONCE_LENGTH_BYTES);
  const aesKey = await importAesKey(key);
  try {
    return new Uint8Array(
      await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: nonce as BufferSource },
        aesKey,
        ciphertext as BufferSource,
      ),
    );
  } catch {
    throw new WrongPasswordOrCorruptDataError(
      "AEAD authentication failed: wrong key or corrupt data",
    );
  }
}

export async function wrapDekToBundle(
  dek: Uint8Array,
  password: string,
  keyEpoch: number,
): Promise<KeyBundle> {
  const parameters = generateKdfParameters();
  const kek = await deriveKek(password, parameters);
  const wrapped = await aeadEncrypt(kek, dek);
  return {
    kdf_salt: parameters.saltB64,
    kdf_time_cost: parameters.timeCost,
    kdf_memory_kib: parameters.memoryKib,
    kdf_parallelism: parameters.parallelism,
    wrapped_dek: bytesToBase64(wrapped),
    key_epoch: keyEpoch,
  };
}

// Recover the DEK from a bundle. Two distinguishable failure shapes:
// - MalformedCiphertextError: the bundle itself is damaged (undecodable
//   base64, blob too short) -- re-typing the password cannot help.
// - WrongPasswordOrCorruptDataError: the AEAD tag failed -- a wrong password
//   and a tampered-but-well-shaped blob are cryptographically
//   indistinguishable, so this is the "wrong password" signal.
export async function unwrapBundle(
  bundle: KeyBundle,
  password: string,
): Promise<Uint8Array> {
  let wrappedDek: Uint8Array;
  try {
    wrappedDek = base64ToBytes(bundle.wrapped_dek);
  } catch {
    throw new MalformedCiphertextError(
      "The stored key bundle's wrapped DEK is not valid base64",
    );
  }
  const kek = await deriveKek(password, {
    saltB64: bundle.kdf_salt,
    timeCost: bundle.kdf_time_cost,
    memoryKib: bundle.kdf_memory_kib,
    parallelism: bundle.kdf_parallelism,
  });
  return aeadDecrypt(kek, wrappedDek);
}

const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

// The decrypted contents of a record's encrypted_secrets blob (the desktop's
// WorkspaceSecretsPayload). Serialized with the same field order + compact
// separators as pydantic's model_dump_json so plaintext digests agree.
export interface WorkspaceSecretsPayload {
  restic_env: string | null;
  ssh_private_key: string | null;
  ssh_known_hosts: string | null;
}

export function serializeSecretsPayload(
  payload: WorkspaceSecretsPayload,
): string {
  return JSON.stringify({
    restic_env: payload.restic_env,
    ssh_private_key: payload.ssh_private_key,
    ssh_known_hosts: payload.ssh_known_hosts,
  });
}

export async function encryptSecretsPayload(
  dek: Uint8Array,
  payload: WorkspaceSecretsPayload,
): Promise<string> {
  const blob = await aeadEncrypt(
    dek,
    textEncoder.encode(serializeSecretsPayload(payload)),
  );
  return bytesToBase64(blob);
}

export async function decryptSecretsPayload(
  dek: Uint8Array,
  encryptedB64: string,
): Promise<WorkspaceSecretsPayload> {
  const plaintext = await aeadDecrypt(dek, base64ToBytes(encryptedB64));
  const parsed = JSON.parse(textDecoder.decode(plaintext)) as Record<
    string,
    unknown
  >;
  return {
    restic_env: (parsed.restic_env as string | null) ?? null,
    ssh_private_key: (parsed.ssh_private_key as string | null) ?? null,
    ssh_known_hosts: (parsed.ssh_known_hosts as string | null) ?? null,
  };
}
