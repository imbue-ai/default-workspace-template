// Cross-language wire-compat tests against Python-generated vectors
// (scripts/generate_crypto_vectors.py), plus self-roundtrips.

import { describe, expect, it } from "vitest";
import vectors from "./test_vectors.json";
import {
  aeadDecrypt,
  aeadEncrypt,
  base64ToBytes,
  bytesToBase64,
  decryptSecretsPayload,
  deriveKek,
  encryptSecretsPayload,
  MalformedCiphertextError,
  serializeSecretsPayload,
  unwrapBundle,
  WrongPasswordOrCorruptDataError,
  wrapDekToBundle,
} from "./secretbox";

const textDecoder = new TextDecoder();

describe("argon2id KEK derivation", () => {
  it("matches the Python-derived KEK byte for byte", async () => {
    const kek = await deriveKek(vectors.kdf.password, {
      saltB64: vectors.kdf.salt_b64,
      timeCost: vectors.kdf.time_cost,
      memoryKib: vectors.kdf.memory_kib,
      parallelism: vectors.kdf.parallelism,
    });
    expect(bytesToBase64(kek)).toBe(vectors.kdf.kek_b64);
  });
});

describe("DEK wrapping", () => {
  it("unwraps a Python-wrapped DEK", async () => {
    const dek = await unwrapBundle(
      {
        kdf_salt: vectors.kdf.salt_b64,
        kdf_time_cost: vectors.kdf.time_cost,
        kdf_memory_kib: vectors.kdf.memory_kib,
        kdf_parallelism: vectors.kdf.parallelism,
        wrapped_dek: vectors.bundle.wrapped_dek_b64,
        key_epoch: 1,
      },
      vectors.kdf.password,
    );
    expect(bytesToBase64(dek)).toBe(vectors.bundle.dek_b64);
  });

  it("rejects the wrong password with the typed error", async () => {
    await expect(
      unwrapBundle(
        {
          kdf_salt: vectors.kdf.salt_b64,
          kdf_time_cost: vectors.kdf.time_cost,
          kdf_memory_kib: vectors.kdf.memory_kib,
          kdf_parallelism: vectors.kdf.parallelism,
          wrapped_dek: vectors.bundle.wrapped_dek_b64,
          key_epoch: 1,
        },
        "not the password",
      ),
    ).rejects.toBeInstanceOf(WrongPasswordOrCorruptDataError);
  });

  it("roundtrips a TS-wrapped bundle", async () => {
    const dek = base64ToBytes(vectors.bundle.dek_b64);
    const bundle = await wrapDekToBundle(dek, "hunter22", 3);
    expect(bundle.key_epoch).toBe(3);
    const unwrapped = await unwrapBundle(bundle, "hunter22");
    expect(bytesToBase64(unwrapped)).toBe(vectors.bundle.dek_b64);
  });
});

// A wrong password and a damaged bundle need different UI remedies (retype vs
// reset), so the two failure shapes must stay typed and distinguishable.
describe("wrong password vs corrupt bundle", () => {
  function pythonBundle(wrappedDek: string) {
    return {
      kdf_salt: vectors.kdf.salt_b64,
      kdf_time_cost: vectors.kdf.time_cost,
      kdf_memory_kib: vectors.kdf.memory_kib,
      kdf_parallelism: vectors.kdf.parallelism,
      wrapped_dek: wrappedDek,
      key_epoch: 1,
    };
  }

  it("reports a non-base64 wrapped DEK as a malformed bundle, not a wrong password", async () => {
    await expect(
      unwrapBundle(pythonBundle("!!! not base64 !!!"), vectors.kdf.password),
    ).rejects.toBeInstanceOf(MalformedCiphertextError);
  });

  it("reports a truncated wrapped DEK as a malformed bundle, not a wrong password", async () => {
    // Shorter than the 12-byte AEAD nonce: structurally impossible, so the
    // bundle is damaged regardless of what password is typed.
    const truncated = bytesToBase64(
      base64ToBytes(vectors.bundle.wrapped_dek_b64).slice(0, 8),
    );
    await expect(
      unwrapBundle(pythonBundle(truncated), vectors.kdf.password),
    ).rejects.toBeInstanceOf(MalformedCiphertextError);
  });

  it("reports a tampered-but-well-shaped blob as the wrong-password error (indistinguishable by design)", async () => {
    const blob = base64ToBytes(vectors.bundle.wrapped_dek_b64);
    blob[blob.length - 1] ^= 0xff;
    await expect(
      unwrapBundle(pythonBundle(bytesToBase64(blob)), vectors.kdf.password),
    ).rejects.toBeInstanceOf(WrongPasswordOrCorruptDataError);
  });
});

describe("secrets blobs", () => {
  it("decrypts a Python-encrypted secrets blob", async () => {
    const dek = base64ToBytes(vectors.bundle.dek_b64);
    const plaintext = await aeadDecrypt(
      dek,
      base64ToBytes(vectors.secrets.blob_b64),
    );
    expect(textDecoder.decode(plaintext)).toBe(vectors.secrets.plaintext);
  });

  it("serializes the payload with pydantic's field order + separators", () => {
    const serialized = serializeSecretsPayload({
      restic_env: "export RESTIC_REPOSITORY=s3:endpoint/bucket\n",
      ssh_private_key:
        "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n",
      ssh_known_hosts: null,
    });
    expect(serialized).toBe(vectors.secrets.plaintext);
  });

  it("roundtrips a TS-encrypted payload", async () => {
    const dek = base64ToBytes(vectors.bundle.dek_b64);
    const encrypted = await encryptSecretsPayload(dek, {
      restic_env: null,
      ssh_private_key: "key material",
      ssh_known_hosts: "example.com ssh-ed25519 AAAA",
    });
    const decrypted = await decryptSecretsPayload(dek, encrypted);
    expect(decrypted).toEqual({
      restic_env: null,
      ssh_private_key: "key material",
      ssh_known_hosts: "example.com ssh-ed25519 AAAA",
    });
  });

  it("nonce-prefixes AEAD blobs (12 bytes + ciphertext + 16-byte tag)", async () => {
    const key = base64ToBytes(vectors.bundle.dek_b64);
    const blob = await aeadEncrypt(key, new Uint8Array([1, 2, 3]));
    expect(blob.length).toBe(12 + 3 + 16);
  });
});
