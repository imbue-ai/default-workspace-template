// Ed25519 for the workspace SSH keypair and the owner-exec signed envelope.
//
// WebCrypto's Ed25519 support is still uneven across browsers, so signing and
// key derivation go through @noble/ed25519 (pure JS, audited) with WebCrypto
// SHA-512 supplying the digest. The OpenSSH encodings here are the wire
// contracts: the public key line lands in the workspace's authorized_keys
// (via the claim's key injection) and the private key travels inside the
// encrypted sync record so the desktop can materialize it for plain SSH.

import * as ed from "@noble/ed25519";
import { bytesToBase64, randomBytes } from "./secretbox";

const textEncoder = new TextEncoder();

export interface Ed25519Keypair {
  seed: Uint8Array;
  publicKey: Uint8Array;
}

export async function generateKeypair(): Promise<Ed25519Keypair> {
  const seed = randomBytes(32);
  return keypairFromSeed(seed);
}

export async function keypairFromSeed(
  seed: Uint8Array,
): Promise<Ed25519Keypair> {
  const publicKey = await ed.getPublicKeyAsync(seed);
  return { seed, publicKey };
}

function writeSshString(chunks: Uint8Array[], value: Uint8Array): void {
  const length = new Uint8Array(4);
  new DataView(length.buffer).setUint32(0, value.length);
  chunks.push(length, value);
}

function concatBytes(chunks: Uint8Array[]): Uint8Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.length;
  }
  return merged;
}

function sshWirePublicKey(publicKey: Uint8Array): Uint8Array {
  const chunks: Uint8Array[] = [];
  writeSshString(chunks, textEncoder.encode("ssh-ed25519"));
  writeSshString(chunks, publicKey);
  return concatBytes(chunks);
}

// The authorized_keys / id_ed25519.pub line for this keypair.
export function opensshPublicKeyLine(
  publicKey: Uint8Array,
  comment: string,
): string {
  const encoded = bytesToBase64(sshWirePublicKey(publicKey));
  return comment
    ? `ssh-ed25519 ${encoded} ${comment}`
    : `ssh-ed25519 ${encoded}`;
}

// Encode the keypair in the unencrypted "openssh-key-v1" container (the
// format `ssh-keygen -t ed25519` writes), so the desktop can hand the synced
// key straight to ssh/paramiko.
export function opensshPrivateKeyPem(
  keypair: Ed25519Keypair,
  comment: string,
): string {
  const authMagic = textEncoder.encode("openssh-key-v1\0");
  const publicKeyBlob = sshWirePublicKey(keypair.publicKey);

  // checkint appears twice so a decryption failure is detectable; the key is
  // unencrypted, so any random value works.
  const checkBytes = randomBytes(4);
  const checkint = concatBytes([checkBytes, checkBytes]);

  const privateSection: Uint8Array[] = [checkint];
  writeSshString(privateSection, textEncoder.encode("ssh-ed25519"));
  writeSshString(privateSection, keypair.publicKey);
  const seedAndPublic = concatBytes([keypair.seed, keypair.publicKey]);
  writeSshString(privateSection, seedAndPublic);
  writeSshString(privateSection, textEncoder.encode(comment));
  let privateBlob = concatBytes(privateSection);

  // Pad the private section to the cipher block size (8 for "none") with the
  // bytes 1, 2, 3, ...
  const padLength = (8 - (privateBlob.length % 8)) % 8;
  if (padLength > 0) {
    const padding = new Uint8Array(padLength);
    for (let i = 0; i < padLength; i++) padding[i] = i + 1;
    privateBlob = concatBytes([privateBlob, padding]);
  }

  const container: Uint8Array[] = [authMagic];
  writeSshString(container, textEncoder.encode("none"));
  writeSshString(container, textEncoder.encode("none"));
  writeSshString(container, new Uint8Array(0));
  const nkeys = new Uint8Array(4);
  new DataView(nkeys.buffer).setUint32(0, 1);
  container.push(nkeys);
  writeSshString(container, publicKeyBlob);
  writeSshString(container, privateBlob);

  const body = bytesToBase64(concatBytes(container));
  const wrapped = body.match(/.{1,70}/g)?.join("\n") ?? body;
  return `-----BEGIN OPENSSH PRIVATE KEY-----\n${wrapped}\n-----END OPENSSH PRIVATE KEY-----\n`;
}

async function sha256Hex(data: Uint8Array): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", data as BufferSource),
  );
  return Array.from(digest)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

// The canonical bytes an owner-exec request signs; must match the dwt
// service's owner_exec.signing.build_signing_string exactly.
export async function buildExecSigningString(
  method: string,
  path: string,
  body: Uint8Array,
  audience: string,
  timestamp: string,
  nonce: string,
): Promise<string> {
  const bodyDigest = await sha256Hex(body);
  return [
    "v1",
    method.toUpperCase(),
    path,
    bodyDigest,
    audience,
    timestamp,
    nonce,
  ].join("\n");
}

export interface ExecEnvelopeHeaders {
  "X-Exec-Signature": string;
  "X-Exec-Public-Key": string;
  "X-Exec-Timestamp": string;
  "X-Exec-Nonce": string;
}

export async function signExecEnvelope(
  keypair: Ed25519Keypair,
  method: string,
  path: string,
  body: Uint8Array,
  audience: string,
  timestamp: string,
  nonce: string,
): Promise<ExecEnvelopeHeaders> {
  const signingString = await buildExecSigningString(
    method,
    path,
    body,
    audience,
    timestamp,
    nonce,
  );
  const signature = await ed.signAsync(
    textEncoder.encode(signingString),
    keypair.seed,
  );
  return {
    "X-Exec-Signature": bytesToBase64(signature),
    "X-Exec-Public-Key": opensshPublicKeyLine(keypair.publicKey, "minds-web"),
    "X-Exec-Timestamp": timestamp,
    "X-Exec-Nonce": nonce,
  };
}
