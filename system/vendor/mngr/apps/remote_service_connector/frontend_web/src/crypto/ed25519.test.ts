// owner-exec RFC 9421/9530 strict-profile tests against the shared vectors
// (vendored from the imbue-ai/owner-exec repo), plus OpenSSH-encoding checks.

import * as ed from "@noble/ed25519";
import { describe, expect, it } from "vitest";
import ownerExecVectors from "./owner_exec_vectors.json";
import { base64ToBytes, bytesToBase64 } from "./secretbox";
import {
  generateKeypair,
  keypairFromSeed,
  opensshPrivateKeyPem,
  opensshPublicKeyLine,
  parseOpensshEd25519PublicKeyLine,
  pinnedHostKeyFromLine,
  requestSignatureBase,
  signExecRequest,
  sshFingerprint,
  verifyExecResponse,
  verifyExecStreamTrailer,
} from "./ed25519";

const textEncoder = new TextEncoder();

function headersFrom(map: Record<string, string>): Headers {
  const headers = new Headers();
  for (const [key, value] of Object.entries(map)) headers.set(key, value);
  return headers;
}

// The created/expires/nonce/keyid parameters of a request Signature-Input.
function signatureInputParams(input: string): {
  created: number;
  expires: number;
  nonce: string;
  keyId: string;
} {
  return {
    created: Number(/;created=(\d+)/.exec(input)?.[1]),
    expires: Number(/;expires=(\d+)/.exec(input)?.[1]),
    nonce: /;nonce="([^"]*)"/.exec(input)?.[1] ?? "",
    keyId: /;keyid="([^"]*)"/.exec(input)?.[1] ?? "",
  };
}

// The raw signature bytes of a `sig1=:base64:` Signature header value.
function signatureBytesFrom(signatureHeader: string): Uint8Array {
  return base64ToBytes(signatureHeader.trim().slice("sig1=:".length, -1));
}

// Rebuild a request's signature base from its headers, assert the rebuilt
// Signature-Input matches byte-for-byte, and verify the Ed25519 signature
// over the base with the given public key.
async function expectRequestSignatureVerifies(
  method: string,
  path: string,
  headers: Record<string, string>,
  publicKey: Uint8Array,
): Promise<void> {
  const input = headers["Signature-Input"];
  const { created, expires, nonce, keyId } = signatureInputParams(input);
  const { signatureInput, base } = requestSignatureBase(
    method,
    path,
    headers["Content-Digest"],
    headers["X-Exec-Audience"],
    headers["X-Exec-Public-Key"],
    created,
    expires,
    nonce,
    keyId,
  );
  expect(signatureInput).toBe(input);
  const signature = signatureBytesFrom(headers.Signature);
  expect(await ed.verifyAsync(signature, base, publicKey)).toBe(true);
}

describe("owner-exec request vectors", () => {
  // Only the valid vectors: the invalid ones exercise verifier *policy*
  // (authorized keys, audience, expiry), which the Go server implements; the
  // TS side only signs, so what must match is the signature-base construction.
  for (const vector of ownerExecVectors.requests.filter((v) => v.expect_valid)) {
    it(`rebuilds ${vector.name}'s signature base byte-for-byte`, async () => {
      await expectRequestSignatureVerifies(
        vector.method,
        new URL(vector.url).pathname,
        vector.headers,
        parseOpensshEd25519PublicKeyLine(vector.headers["X-Exec-Public-Key"]),
      );
    });
  }
});

describe("owner-exec response vectors", () => {
  for (const vector of ownerExecVectors.responses) {
    it(`verifies ${vector.name} as ${vector.expect_valid}`, async () => {
      const hostKey = await pinnedHostKeyFromLine(vector.host_key_line);
      const body = base64ToBytes(vector.body_b64);
      const request = {
        method: vector.request_method,
        path: new URL(vector.request_url).pathname,
        signatureHeader: vector.request_headers.Signature,
      };
      const verify = () =>
        verifyExecResponse(
          vector.status_code,
          headersFrom(vector.headers),
          body,
          request,
          hostKey,
          vector.verify_at,
        );
      if (vector.expect_valid) {
        await expect(verify()).resolves.toBeUndefined();
      } else {
        await expect(verify()).rejects.toThrow();
      }
    });
  }
});

describe("owner-exec stream-trailer vectors", () => {
  for (const vector of ownerExecVectors.streams) {
    it(`verifies ${vector.name} as ${vector.expect_valid}`, async () => {
      const hostKey = await pinnedHostKeyFromLine(vector.host_key_line);
      const streamBytes = base64ToBytes(vector.stream_bytes_b64);
      const trailer = {
        type: "signature" as const,
        created: vector.created,
        keyid: vector.host_key_id,
        tag: "imbue-owner-exec-stream",
        signature: vector.signature,
      };
      const verify = () =>
        verifyExecStreamTrailer(
          streamBytes,
          vector.request_signature,
          trailer,
          hostKey,
          vector.verify_at,
        );
      if (vector.expect_valid) {
        await expect(verify()).resolves.toBeUndefined();
      } else {
        await expect(verify()).rejects.toThrow();
      }
    });
  }
});

describe("owner-exec fingerprint", () => {
  it("matches the vector host key id", async () => {
    const vector = ownerExecVectors.responses[0];
    const raw = parseOpensshEd25519PublicKeyLine(vector.host_key_line);
    expect(await sshFingerprint(raw)).toBe(vector.host_key_id);
  });
});

describe("owner-exec request signing", () => {
  it("produces profile headers whose signature verifies over the rebuilt base", async () => {
    const keypair = await generateKeypair();
    const body = textEncoder.encode(JSON.stringify({ command: ["true"] }));
    const { headers } = await signExecRequest(
      keypair,
      "POST",
      "/run",
      body,
      "vm:host-xyz",
      1755300000,
      "nonce-a-sufficiently-long-value",
    );
    expect(headers["Signature-Input"]).toContain('tag="imbue-owner-exec"');
    expect(headers["Signature-Input"]).toContain(
      '("@method" "@path" "content-digest" "x-exec-audience" "x-exec-public-key")',
    );
    expect(headers["X-Exec-Audience"]).toBe("vm:host-xyz");
    expect(headers["Content-Digest"]).toMatch(/^sha-256=:.+:$/);
    await expectRequestSignatureVerifies(
      "POST",
      "/run",
      { ...headers },
      keypair.publicKey,
    );
  });
});

describe("OpenSSH encodings", () => {
  it("round-trips a public key line through parse", async () => {
    const keypair = await keypairFromSeed(new Uint8Array(32).fill(7));
    const line = opensshPublicKeyLine(keypair.publicKey, "");
    const parsed = parseOpensshEd25519PublicKeyLine(line);
    expect(bytesToBase64(parsed)).toBe(bytesToBase64(keypair.publicKey));
  });

  it("emits a parseable openssh-key-v1 private key container", async () => {
    const keypair = await keypairFromSeed(new Uint8Array(32).fill(9));
    const pem = opensshPrivateKeyPem(keypair, "minds-web");
    expect(pem.startsWith("-----BEGIN OPENSSH PRIVATE KEY-----\n")).toBe(true);
    expect(pem.endsWith("-----END OPENSSH PRIVATE KEY-----\n")).toBe(true);
    const body = pem
      .split("\n")
      .filter((line) => !line.startsWith("-----"))
      .join("");
    const raw = base64ToBytes(body);
    expect(new TextDecoder().decode(raw.slice(0, 15))).toBe("openssh-key-v1\0");
  });
});
