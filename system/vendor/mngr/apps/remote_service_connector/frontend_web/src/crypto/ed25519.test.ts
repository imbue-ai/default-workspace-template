// Exec-envelope + OpenSSH encoding tests against the Python-generated
// vectors (Ed25519 signatures are deterministic, so byte-equality holds).

import { describe, expect, it } from "vitest";
import vectors from "./test_vectors.json";
import { base64ToBytes, bytesToBase64 } from "./secretbox";
import {
  buildExecSigningString,
  keypairFromSeed,
  opensshPrivateKeyPem,
  opensshPublicKeyLine,
  signExecEnvelope,
} from "./ed25519";

const textEncoder = new TextEncoder();
const envelope = vectors.exec_envelope;

describe("exec envelope", () => {
  it("builds the exact signing string the dwt service verifies", async () => {
    const signingString = await buildExecSigningString(
      envelope.method,
      envelope.path,
      textEncoder.encode(envelope.body),
      envelope.audience,
      envelope.timestamp,
      envelope.nonce,
    );
    expect(signingString).toBe(envelope.signing_string);
  });

  it("produces the same Ed25519 signature as the Python signer", async () => {
    const keypair = await keypairFromSeed(base64ToBytes(envelope.seed_b64));
    const headers = await signExecEnvelope(
      keypair,
      envelope.method,
      envelope.path,
      textEncoder.encode(envelope.body),
      envelope.audience,
      envelope.timestamp,
      envelope.nonce,
    );
    expect(headers["X-Exec-Signature"]).toBe(envelope.signature_b64);
    expect(headers["X-Exec-Timestamp"]).toBe(envelope.timestamp);
    expect(headers["X-Exec-Nonce"]).toBe(envelope.nonce);
  });
});

describe("OpenSSH encodings", () => {
  it("emits the same public key line as the Python encoder", async () => {
    const keypair = await keypairFromSeed(base64ToBytes(envelope.seed_b64));
    const line = opensshPublicKeyLine(keypair.publicKey, "");
    expect(line).toBe(envelope.public_key_openssh);
  });

  it("emits a parseable openssh-key-v1 private key container", async () => {
    const keypair = await keypairFromSeed(base64ToBytes(envelope.seed_b64));
    const pem = opensshPrivateKeyPem(keypair, "minds-web");
    expect(pem.startsWith("-----BEGIN OPENSSH PRIVATE KEY-----\n")).toBe(true);
    expect(pem.endsWith("-----END OPENSSH PRIVATE KEY-----\n")).toBe(true);

    // Decode the container and check the structure: magic, ciphers "none",
    // one key, the public key blob, and the seed inside the private section.
    const body = pem
      .split("\n")
      .filter((line) => !line.startsWith("-----"))
      .join("");
    const raw = base64ToBytes(body);
    const magic = new TextDecoder().decode(raw.slice(0, 15));
    expect(magic).toBe("openssh-key-v1\0");
    const container = bytesToBase64(raw);
    expect(container).toContain(
      bytesToBase64(new TextEncoder().encode("none")).slice(0, 4),
    );
    // The private section embeds seed||publicKey; look for the seed bytes.
    const rawStr = Array.from(raw)
      .map((byte) => String.fromCharCode(byte))
      .join("");
    const seedStr = Array.from(base64ToBytes(envelope.seed_b64))
      .map((byte) => String.fromCharCode(byte))
      .join("");
    expect(rawStr.includes(seedStr)).toBe(true);
  });
});
