// Ed25519 for the workspace SSH keypair and the owner-exec signed envelope.
//
// WebCrypto's Ed25519 support is still uneven across browsers, so signing and
// key derivation go through @noble/ed25519 (pure JS, audited) with WebCrypto
// SHA-512 supplying the digest. The OpenSSH encodings here are the wire
// contracts: the public key line lands in the workspace's authorized_keys
// (via the claim's key injection) and the private key travels inside the
// encrypted sync record so the desktop can materialize it for plain SSH.

import * as ed from "@noble/ed25519";
import { base64ToBytes, bytesToBase64, randomBytes } from "./secretbox";

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

// ---------------------------------------------------------------------------
// owner-exec RFC 9421 / RFC 9530 strict profile
//
// Mirrors the owner-exec repo's spec/profile.md and internal/profile, and the
// Python client in imbue_common.owner_exec_client. Cross-checked by the shared
// vectors (see crypto/owner_exec_vectors.json and ed25519.test.ts).
// ---------------------------------------------------------------------------

export const REQUEST_TAG = "imbue-owner-exec";
export const RESPONSE_TAG = "imbue-owner-exec-resp";
export const STREAM_TAG = "imbue-owner-exec-stream";
export const CREATED_WINDOW_SECONDS = 60;
export const RESPONSE_CREATED_WINDOW_SECONDS = 300;

const REQUEST_COMPONENTS = [
  "@method",
  "@path",
  "content-digest",
  "x-exec-audience",
  "x-exec-public-key",
] as const;

function sfString(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

async function sha256(data: Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(
    await crypto.subtle.digest("SHA-256", data as BufferSource),
  );
}

// The RFC 9530 sha-256 Content-Digest header value for a body.
export async function contentDigest(body: Uint8Array): Promise<string> {
  return `sha-256=:${bytesToBase64(await sha256(body))}:`;
}

// The OpenSSH SHA256 fingerprint (keyid) of an Ed25519 public key.
export async function sshFingerprint(publicKey: Uint8Array): Promise<string> {
  const wire = sshWirePublicKey(publicKey);
  const digest = bytesToBase64(await sha256(wire)).replace(/=+$/, "");
  return `SHA256:${digest}`;
}

// Parse an "ssh-ed25519 AAAA... [comment]" line into the raw 32-byte key.
export function parseOpensshEd25519PublicKeyLine(line: string): Uint8Array {
  const parts = line.trim().split(/\s+/);
  if (parts.length < 2 || parts[0] !== "ssh-ed25519") {
    throw new Error("not an ssh-ed25519 public key line");
  }
  const wire = base64ToBytes(parts[1]);
  // wire = string("ssh-ed25519") || string(rawKey); skip the first sf-string.
  const view = new DataView(wire.buffer, wire.byteOffset, wire.byteLength);
  const typeLen = view.getUint32(0);
  const rawOffset = 4 + typeLen + 4;
  return wire.slice(rawOffset, rawOffset + 32);
}

// Exported for the vector tests, which rebuild the base to cross-check the
// construction against the Go signer's output.
export function requestSignatureBase(
  method: string,
  path: string,
  digestValue: string,
  audience: string,
  publicKeyLine: string,
  created: number,
  expires: number,
  nonce: string,
  keyId: string,
): { signatureInput: string; base: Uint8Array } {
  const params =
    `(${REQUEST_COMPONENTS.map(sfString).join(" ")})` +
    `;created=${created};expires=${expires};nonce=${sfString(nonce)}` +
    `;tag=${sfString(REQUEST_TAG)};keyid=${sfString(keyId)}`;
  const lines = [
    `"@method": ${method.toUpperCase()}`,
    `"@path": ${path}`,
    `"content-digest": ${digestValue}`,
    `"x-exec-audience": ${audience}`,
    `"x-exec-public-key": ${publicKeyLine.trim()}`,
    `"@signature-params": ${params}`,
  ];
  return {
    signatureInput: `sig1=${params}`,
    base: textEncoder.encode(lines.join("\n")),
  };
}

export interface ExecRequestHeaders {
  "Content-Digest": string;
  "X-Exec-Audience": string;
  "X-Exec-Public-Key": string;
  "Signature-Input": string;
  Signature: string;
}

// Sign one owner-exec request per the strict profile. `nowUnix` is injectable
// for tests; production passes Math.floor(Date.now() / 1000).
export async function signExecRequest(
  keypair: Ed25519Keypair,
  method: string,
  path: string,
  body: Uint8Array,
  audience: string,
  nowUnix: number,
  nonce: string,
): Promise<{ headers: ExecRequestHeaders; signatureB64: string }> {
  const publicKeyLine = opensshPublicKeyLine(keypair.publicKey, "");
  const keyId = await sshFingerprint(keypair.publicKey);
  const digestValue = await contentDigest(body);
  const created = nowUnix;
  const expires = created + CREATED_WINDOW_SECONDS;
  const { signatureInput, base } = requestSignatureBase(
    method,
    path,
    digestValue,
    audience,
    publicKeyLine,
    created,
    expires,
    nonce,
    keyId,
  );
  const signature = await ed.signAsync(base, keypair.seed);
  const signatureB64 = bytesToBase64(signature);
  return {
    headers: {
      "Content-Digest": digestValue,
      "X-Exec-Audience": audience,
      "X-Exec-Public-Key": publicKeyLine.trim(),
      "Signature-Input": signatureInput,
      Signature: `sig1=:${signatureB64}:`,
    },
    signatureB64,
  };
}

// The exact `:base64:` serialization of a request's sig1 signature member,
// used to bind a response to the request.
export function requestSignatureMember(signatureHeader: string): string {
  const value = signatureHeader.trim();
  if (!value.startsWith("sig1=:") || !value.endsWith(":")) {
    throw new Error("Signature header is not a single sig1 byte-sequence");
  }
  return value.slice("sig1=".length);
}

function responseSignatureBase(
  statusCode: number,
  digestValue: string,
  requestMethod: string,
  requestPath: string,
  requestSignatureMemberValue: string,
  created: number,
  keyId: string,
): Uint8Array {
  const params =
    '("@status" "content-digest" "@method";req "@path";req "signature";key="sig1";req)' +
    `;created=${created};tag=${sfString(RESPONSE_TAG)};keyid=${sfString(keyId)}`;
  const lines = [
    `"@status": ${statusCode}`,
    `"content-digest": ${digestValue}`,
    `"@method";req: ${requestMethod.toUpperCase()}`,
    `"@path";req: ${requestPath}`,
    `"signature";key="sig1";req: ${requestSignatureMemberValue}`,
    `"@signature-params": ${params}`,
  ];
  return textEncoder.encode(lines.join("\n"));
}

function streamTrailerBase(
  streamDigest: Uint8Array,
  requestSignatureB64: string,
  keyId: string,
  created: number,
): Uint8Array {
  const lines = [
    `"stream-digest": sha-256=:${bytesToBase64(streamDigest)}:`,
    `"request-signature": :${requestSignatureB64}:`,
    '"@signature-params": ("stream-digest" "request-signature")' +
      `;created=${created};keyid=${sfString(keyId)};tag=${sfString(STREAM_TAG)}`,
  ];
  return textEncoder.encode(lines.join("\n"));
}

function parseParam(signatureInput: string, name: string): string | null {
  const match = signatureInput.match(
    new RegExp(`;${name}=("?)([^;"]*)\\1`),
  );
  return match ? match[2] : null;
}

// The endpoint's pinned host key, from the claim response / synced record.
export interface PinnedHostKey {
  publicKey: Uint8Array;
  keyId: string;
}

export async function pinnedHostKeyFromLine(
  line: string,
): Promise<PinnedHostKey> {
  const publicKey = parseOpensshEd25519PublicKeyLine(line);
  return { publicKey, keyId: await sshFingerprint(publicKey) };
}

// Verify a signed owner-exec response against the pinned host key, bound to the
// request. Throws on any failure (fail closed).
export async function verifyExecResponse(
  statusCode: number,
  responseHeaders: Headers,
  responseBody: Uint8Array,
  request: { method: string; path: string; signatureHeader: string },
  hostKey: PinnedHostKey,
  nowUnix: number,
): Promise<void> {
  const signatureInput = responseHeaders.get("Signature-Input");
  const signatureHeader = responseHeaders.get("Signature");
  const digestHeader = responseHeaders.get("Content-Digest");
  if (!signatureInput || !signatureHeader || !digestHeader) {
    throw new Error("response is missing signature/digest headers");
  }
  if (!signatureInput.includes(`tag="${RESPONSE_TAG}"`)) {
    throw new Error("response signature tag is wrong");
  }
  if (!signatureInput.includes(`keyid="${hostKey.keyId}"`)) {
    throw new Error("response keyid does not match the pinned host key");
  }
  const created = Number(parseParam(signatureInput, "created"));
  if (Math.abs(nowUnix - created) > RESPONSE_CREATED_WINDOW_SECONDS) {
    throw new Error("response created timestamp is outside the window");
  }
  if (digestHeader.trim() !== (await contentDigest(responseBody))) {
    throw new Error("response Content-Digest does not match the body");
  }
  const base = responseSignatureBase(
    statusCode,
    digestHeader,
    request.method,
    request.path,
    requestSignatureMember(request.signatureHeader),
    created,
    hostKey.keyId,
  );
  const signature = base64ToBytes(
    signatureHeader.trim().slice("sig1=:".length, -1),
  );
  if (!(await ed.verifyAsync(signature, base, hostKey.publicKey))) {
    throw new Error("response signature does not verify");
  }
}

export interface ExecStreamTrailer {
  type: "signature";
  created: number;
  keyid: string;
  tag: string;
  signature: string;
}

// Verify a /run stream trailer against the pinned host key. Throws on failure.
export async function verifyExecStreamTrailer(
  streamBytes: Uint8Array,
  requestSignatureB64: string,
  trailer: ExecStreamTrailer,
  hostKey: PinnedHostKey,
  nowUnix: number,
): Promise<void> {
  if (trailer.tag !== STREAM_TAG) throw new Error("stream trailer tag is wrong");
  if (trailer.keyid !== hostKey.keyId) {
    throw new Error("stream trailer keyid does not match the pinned host key");
  }
  if (Math.abs(nowUnix - trailer.created) > RESPONSE_CREATED_WINDOW_SECONDS) {
    throw new Error("stream trailer created timestamp is outside the window");
  }
  const base = streamTrailerBase(
    await sha256(streamBytes),
    requestSignatureB64,
    hostKey.keyId,
    trailer.created,
  );
  const signature = base64ToBytes(trailer.signature);
  if (!(await ed.verifyAsync(signature, base, hostKey.publicKey))) {
    throw new Error("stream trailer signature does not verify");
  }
}
