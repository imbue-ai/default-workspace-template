#!/usr/bin/env node
// wsvisit: a visiting agent's helper for one Imbue Studio workspace.
//
// Published in the template repository at the exact version the workspace runs
// (the invite names that address and the file's hash) and served by the
// workspace itself at GET <auth origin>/_agent/helper as a fallback; the join
// answer carries the hash too, and this file warns when its own differs. One
// file, Node 18 or newer, no dependencies: `crypto` signs with the agent's
// Ed25519 key per the owner-exec profile (RFC 9421 + RFC 9530, see
// spec/profile.md in imbue-ai/owner-exec), global `fetch` talks to the share,
// `fs` keeps the key and the join answer under
// ~/.config/imbue-workspace/<workspace domain>/.
//
//   wsvisit join <auth-origin> <code> [--name <name>]
//   wsvisit run [--cwd <dir>] [--timeout <seconds>] -- <argv...>
//   wsvisit read <path>
//   wsvisit write <path> [--from <file>] [--mode <octal>]
//   wsvisit get <app>/<path> | <url>           an app's answer over the share, with your access attached
//   wsvisit primer | skills [--json] | changes [--since <iso>] | me
//   wsvisit invite [--scope workspace|<app,app>] [--expires-in-days <n>] [--name-hint <text>]
//   --workspace <domain> selects a workspace when several are joined.
//
// `get` sends the admission token as a bearer to an origin of this workspace
// only, and waits out an app that answers 503 with Retry-After while it starts.
//
// Two behaviours are the helper's own contract with the agent: it refreshes the
// skills index when its copy is older than a day and prints what changed to
// stderr before the command runs, and it self-heals stale coordinates (a moved
// exec origin, a rotated host key) by re-reading them from the `me` route and
// retrying once.

"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const SIGNATURE_LABEL = "sig1";
const REQUEST_TAG = "imbue-owner-exec";
const RESPONSE_TAG = "imbue-owner-exec-resp";
const STREAM_TAG = "imbue-owner-exec-stream";
const CREATED_WINDOW_SECONDS = 60;
const RESPONSE_CREATED_WINDOW_SECONDS = 300;
const REQUEST_COMPONENTS = ['"@method"', '"@path"', '"content-digest"', '"x-exec-audience"', '"x-exec-public-key"'];
const RESPONSE_COMPONENTS = '("@status" "content-digest" "@method";req "@path";req "signature";key="sig1";req)';
const STREAM_COMPONENTS = '("stream-digest" "request-signature")';
const SKILLS_REFRESH_MS = 24 * 60 * 60 * 1000;
const DEFAULT_RUN_TIMEOUT_SECONDS = 600;
const MAX_RUN_TIMEOUT_SECONDS = 3600;
const REQUEST_TIMEOUT_MS = 60 * 1000;
const EXIT_VERIFICATION_FAILED = 70;
// An idle app the shell stopped starts on its first request and answers 503
// with Retry-After until it is up: a second or two, never minutes.
const GET_MAX_RETRIES = 5;
const GET_MAX_RETRY_DELAY_MS = 5000;
const GET_MAX_REDIRECTS = 5;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);
const EXIT_REVOKED = 77;
const EXIT_USAGE = 64;

// `isRetryable` is false for a failure the self-heal must not answer by running
// the action again: a command whose output already streamed has already run.
class HelperError extends Error {
  constructor(message, exitCode = 1, isRetryable = true) {
    super(message);
    this.exitCode = exitCode;
    this.isRetryable = isRetryable;
  }
}

// Keys and the OpenSSH line

function sshString(bytes) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(bytes.length, 0);
  return Buffer.concat([length, bytes]);
}

function rawPublicKeyFromKeyObject(publicKey) {
  // The raw 32-byte Ed25519 key is the last 32 bytes of the SPKI DER encoding.
  const der = publicKey.export({ type: "spki", format: "der" });
  return der.subarray(der.length - 32);
}

function publicKeyLineFromRaw(raw) {
  const wire = Buffer.concat([sshString(Buffer.from("ssh-ed25519")), sshString(raw)]);
  return `ssh-ed25519 ${wire.toString("base64")}`;
}

function fingerprintOfPublicKeyLine(line) {
  const wire = Buffer.from(line.trim().split(/\s+/)[1], "base64");
  return "SHA256:" + crypto.createHash("sha256").update(wire).digest("base64").replace(/=+$/, "");
}

function publicKeyObjectFromLine(line) {
  const parts = line.trim().split(/\s+/);
  if (parts[0] !== "ssh-ed25519" || !parts[1]) throw new HelperError("not an ssh-ed25519 public key line");
  const wire = Buffer.from(parts[1], "base64");
  let offset = 0;
  const readString = () => {
    const length = wire.readUInt32BE(offset);
    offset += 4;
    const value = wire.subarray(offset, offset + length);
    offset += length;
    return value;
  };
  const type = readString().toString();
  const raw = readString();
  if (type !== "ssh-ed25519" || raw.length !== 32) throw new HelperError("malformed ssh-ed25519 public key");
  return crypto.createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: raw.toString("base64url") }, format: "jwk" });
}

function generateKeypair() {
  const { privateKey, publicKey } = crypto.generateKeyPairSync("ed25519");
  return {
    privateKeyPem: privateKey.export({ type: "pkcs8", format: "pem" }),
    publicKeyLine: publicKeyLineFromRaw(rawPublicKeyFromKeyObject(publicKey)),
  };
}

function privateKeyFromPem(pem) {
  return crypto.createPrivateKey({ key: pem, format: "pem" });
}

// The signing profile

function sfString(value) {
  return `"${String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function contentDigest(body) {
  return `sha-256=:${crypto.createHash("sha256").update(body).digest("base64")}:`;
}

function requestSignatureParams(created, expires, nonce, keyId) {
  return (
    `(${REQUEST_COMPONENTS.join(" ")});created=${created};expires=${expires};nonce=${sfString(nonce)}` +
    `;tag=${sfString(REQUEST_TAG)};keyid=${sfString(keyId)}`
  );
}

function requestSignatureBase(method, requestPath, digestValue, audience, publicKeyLine, params) {
  return [
    `"@method": ${method.toUpperCase()}`,
    `"@path": ${requestPath}`,
    `"content-digest": ${digestValue}`,
    `"x-exec-audience": ${audience}`,
    `"x-exec-public-key": ${publicKeyLine.trim()}`,
    `"@signature-params": ${params}`,
  ].join("\n");
}

function signRequest(keypair, method, requestPath, body, audience, nowUnix, nonce) {
  const keyId = fingerprintOfPublicKeyLine(keypair.publicKeyLine);
  const digestValue = contentDigest(body);
  const created = nowUnix;
  const expires = created + CREATED_WINDOW_SECONDS;
  const chosenNonce = nonce !== undefined ? nonce : crypto.randomBytes(18).toString("base64");
  const params = requestSignatureParams(created, expires, chosenNonce, keyId);
  const base = requestSignatureBase(method, requestPath, digestValue, audience, keypair.publicKeyLine, params);
  const signature = crypto.sign(null, Buffer.from(base, "utf8"), privateKeyFromPem(keypair.privateKeyPem));
  const signatureB64 = signature.toString("base64");
  return {
    headers: {
      "Content-Digest": digestValue,
      "X-Exec-Audience": audience,
      "X-Exec-Public-Key": keypair.publicKeyLine.trim(),
      "Signature-Input": `${SIGNATURE_LABEL}=${params}`,
      Signature: `${SIGNATURE_LABEL}=:${signatureB64}:`,
      "Content-Type": "application/json",
    },
    method,
    path: requestPath,
    signatureB64,
  };
}

function parseSignatureInputParams(signatureInput) {
  // The value is `sig1=(...);created=...;...`; only the parameters are read, and
  // the verifier rebuilds the whole string from the pinned expectations rather
  // than trusting it.
  const prefix = `${SIGNATURE_LABEL}=`;
  if (!signatureInput || !signatureInput.startsWith(prefix)) throw new HelperError("missing sig1 Signature-Input", EXIT_VERIFICATION_FAILED);
  const afterComponents = signatureInput.indexOf(")");
  if (afterComponents < 0) throw new HelperError("malformed Signature-Input", EXIT_VERIFICATION_FAILED);
  const components = signatureInput.slice(prefix.length, afterComponents + 1);
  const params = {};
  const pattern = /;([a-z]+)=("(?:[^"\\]|\\.)*"|\d+)/g;
  let match;
  while ((match = pattern.exec(signatureInput.slice(afterComponents + 1))) !== null) {
    const raw = match[2];
    params[match[1]] = raw.startsWith('"') ? raw.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\") : Number(raw);
  }
  return { components, params };
}

function parseSignatureHeader(signatureHeader) {
  const match = /^sig1=:([A-Za-z0-9+/=]+):$/.exec((signatureHeader || "").trim());
  if (!match) throw new HelperError("missing or malformed sig1 Signature", EXIT_VERIFICATION_FAILED);
  return match[1];
}

function responseParams(created, keyId) {
  return `${RESPONSE_COMPONENTS};created=${created};tag=${sfString(RESPONSE_TAG)};keyid=${sfString(keyId)}`;
}

function responseSignatureBase(statusCode, digestValue, requestMethod, requestPath, requestSignatureB64, created, keyId) {
  return [
    `"@status": ${statusCode}`,
    `"content-digest": ${digestValue}`,
    `"@method";req: ${requestMethod.toUpperCase()}`,
    `"@path";req: ${requestPath}`,
    `"signature";key="sig1";req: :${requestSignatureB64}:`,
    `"@signature-params": ${responseParams(created, keyId)}`,
  ].join("\n");
}

function verifyResponse(statusCode, headers, body, signedRequest, hostPublicKeyLine, nowUnix) {
  const hostKeyId = fingerprintOfPublicKeyLine(hostPublicKeyLine);
  const { components, params } = parseSignatureInputParams(headerValue(headers, "signature-input"));
  if (components !== RESPONSE_COMPONENTS) throw new HelperError("response signature covers the wrong components", EXIT_VERIFICATION_FAILED);
  if (params.tag !== RESPONSE_TAG) throw new HelperError("response signature tag mismatch", EXIT_VERIFICATION_FAILED);
  if (params.keyid !== hostKeyId) throw new HelperError("response signed by an unexpected host key", EXIT_VERIFICATION_FAILED);
  if (typeof params.created !== "number" || Math.abs(nowUnix - params.created) > RESPONSE_CREATED_WINDOW_SECONDS) {
    throw new HelperError("response signature is outside its time window", EXIT_VERIFICATION_FAILED);
  }
  const digestValue = headerValue(headers, "content-digest");
  if (digestValue !== contentDigest(body)) throw new HelperError("response body does not match its Content-Digest", EXIT_VERIFICATION_FAILED);
  const signatureB64 = parseSignatureHeader(headerValue(headers, "signature"));
  const base = responseSignatureBase(statusCode, digestValue, signedRequest.method, signedRequest.path, signedRequest.signatureB64, params.created, hostKeyId);
  const isValid = crypto.verify(null, Buffer.from(base, "utf8"), publicKeyObjectFromLine(hostPublicKeyLine), Buffer.from(signatureB64, "base64"));
  if (!isValid) throw new HelperError("response signature does not verify", EXIT_VERIFICATION_FAILED);
}

function streamTrailerBase(streamSha256, requestSignatureB64, keyId, created) {
  return [
    `"stream-digest": sha-256=:${streamSha256.toString("base64")}:`,
    `"request-signature": :${requestSignatureB64}:`,
    `"@signature-params": ${STREAM_COMPONENTS};created=${created};keyid=${sfString(keyId)};tag=${sfString(STREAM_TAG)}`,
  ].join("\n");
}

function verifyStreamTrailer(trailer, streamSha256, requestSignatureB64, hostPublicKeyLine, nowUnix) {
  const hostKeyId = fingerprintOfPublicKeyLine(hostPublicKeyLine);
  if (!trailer || trailer.type !== "signature") throw new HelperError("the run stream ended without its signature", EXIT_VERIFICATION_FAILED);
  if (trailer.tag !== STREAM_TAG || trailer.keyid !== hostKeyId) throw new HelperError("stream signature tag or key mismatch", EXIT_VERIFICATION_FAILED);
  if (typeof trailer.created !== "number" || Math.abs(nowUnix - trailer.created) > RESPONSE_CREATED_WINDOW_SECONDS) {
    throw new HelperError("stream signature is outside its time window", EXIT_VERIFICATION_FAILED);
  }
  const base = streamTrailerBase(streamSha256, requestSignatureB64, hostKeyId, trailer.created);
  const isValid = crypto.verify(null, Buffer.from(base, "utf8"), publicKeyObjectFromLine(hostPublicKeyLine), Buffer.from(trailer.signature, "base64"));
  if (!isValid) throw new HelperError("stream signature does not verify", EXIT_VERIFICATION_FAILED);
}

function headerValue(headers, name) {
  if (typeof headers.get === "function") return headers.get(name) || "";
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() === wanted) return value;
  return "";
}

// State on disk

function stateRoot() {
  return path.join(process.env.WSVISIT_STATE_DIR || path.join(os.homedir(), ".config", "imbue-workspace"));
}

function workspaceDir(domain) {
  return path.join(stateRoot(), domain);
}

// The token and the private key live here, so the directory is used (read or
// written) only while nobody else can read it.
function requirePrivateDir(dir) {
  const mode = fs.statSync(dir).mode & 0o777;
  if (mode !== 0o700) throw new HelperError(`refusing to use ${dir}: its mode is ${mode.toString(8)}, expected 700`);
}

function ensurePrivateDir(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  requirePrivateDir(dir);
}

function loadConfig(domain) {
  const dir = workspaceDir(domain);
  const configPath = path.join(dir, "config.json");
  if (!fs.existsSync(configPath)) return null;
  requirePrivateDir(dir);
  const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
  config.keypair = { privateKeyPem: fs.readFileSync(path.join(dir, "key.pem"), "utf8"), publicKeyLine: config.public_key_line };
  return config;
}

function saveConfig(config) {
  const dir = workspaceDir(config.workspace.domain);
  ensurePrivateDir(dir);
  const { keypair, ...persisted } = config;
  fs.writeFileSync(path.join(dir, "key.pem"), keypair.privateKeyPem, { mode: 0o600 });
  fs.writeFileSync(path.join(dir, "config.json"), JSON.stringify(persisted, null, 2) + "\n", { mode: 0o600 });
}

function joinedDomains() {
  const root = stateRoot();
  if (!fs.existsSync(root)) return [];
  return fs.readdirSync(root).filter((entry) => fs.existsSync(path.join(root, entry, "config.json")));
}

function resolveConfig(options) {
  const domains = joinedDomains();
  const domain = options.workspace || (domains.length === 1 ? domains[0] : null);
  if (!domain) {
    if (domains.length === 0) throw new HelperError("no workspace joined yet; run: wsvisit join <auth-origin> <code>", EXIT_USAGE);
    throw new HelperError(`several workspaces are joined; pick one with --workspace: ${domains.join(", ")}`, EXIT_USAGE);
  }
  const config = loadConfig(domain);
  if (!config) throw new HelperError(`no workspace ${domain} is joined`, EXIT_USAGE);
  return config;
}

// HTTP

// The share's origins stop answering the moment the workspace is unpublished;
// the grant and the address survive it, so publishing again is all it takes.
const UNREACHABLE_HINT =
  " (the workspace may not be published right now; its owner publishes it from the Share panel, and your access resumes at the same address)";

async function fetchWithTimeout(url, init, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    throw new HelperError(`could not reach ${url}: ${error.message}${UNREACHABLE_HINT}`);
  } finally {
    clearTimeout(timer);
  }
}

function gatewayOrigin(config) {
  return config.gateway_origin || config.workspace.auth_origin;
}

async function gatewayJson(config, method, routePath, body, query) {
  const url = new URL(routePath, gatewayOrigin(config));
  if (query) for (const [key, value] of Object.entries(query)) if (value !== undefined) url.searchParams.set(key, value);
  const response = await fetchWithTimeout(
    url,
    {
      method,
      headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    REQUEST_TIMEOUT_MS,
  );
  const text = await response.text();
  if (response.status === 403) throw new HelperError("the workspace refused this access (revoked, or the workspace moved)", EXIT_REVOKED);
  if (!response.ok) throw new HelperError(`${method} ${routePath} answered ${response.status}: ${text.slice(0, 300)}`);
  return text;
}

async function execRequest(config, routePath, payload, options = {}) {
  if (!config.exec) throw new HelperError("this access covers named apps only; there is no exec authority", EXIT_USAGE);
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  const signed = signRequest(config.keypair, "POST", routePath, body, config.exec.audience, nowUnix());
  const response = await fetchWithTimeout(
    `${config.exec.origin}${routePath}`,
    { method: "POST", headers: { ...signed.headers, Authorization: `Bearer ${config.token}` }, body },
    options.timeoutMs || REQUEST_TIMEOUT_MS,
  );
  if (response.status === 403) throw new HelperError("the workspace refused this access (revoked, or the workspace moved)", EXIT_REVOKED);
  return { response, signed };
}

// A verified answer from owner-exec. A signed 404 is an answer too (a file that
// does not exist), so it is returned rather than thrown: only the daemon's
// refusals and failures are errors.
async function verifiedExecJson(config, routePath, payload) {
  const { response, signed } = await execRequest(config, routePath, payload);
  const body = Buffer.from(await response.arrayBuffer());
  verifyResponse(response.status, response.headers, body, signed, config.exec.host_public_key, nowUnix());
  const parsed = body.length ? JSON.parse(body.toString("utf8")) : {};
  if (!response.ok && response.status !== 404) {
    const detail = parsed.error || body.toString("utf8").slice(0, 300);
    throw new HelperError(`${routePath} answered ${response.status}: ${detail}${clockSkewNote(headerValue(response.headers, "date"), nowUnix())}`);
  }
  return { status: response.status, body: parsed };
}

function nowUnix() {
  return Math.floor(Date.now() / 1000);
}

// The daemon verifies each signature within a 60-second window, so a refusal
// from a machine whose clock is off is explained by the Date header it answered with.
function clockSkewNote(dateHeader, nowUnix) {
  const answeredAt = Date.parse(dateHeader || "");
  if (Number.isNaN(answeredAt)) return "";
  const skewSeconds = Math.round(answeredAt / 1000 - nowUnix);
  if (Math.abs(skewSeconds) <= CREATED_WINDOW_SECONDS) return "";
  const direction = skewSeconds > 0 ? "ahead of" : "behind";
  return ` (the workspace's clock is ${Math.abs(skewSeconds)}s ${direction} this machine's; signatures are only accepted within ${CREATED_WINDOW_SECONDS}s, so set this machine's clock)`;
}

// Stale coordinates (a moved exec origin, a rotated host key, a re-registered
// service) heal by re-reading `me` and retrying once; a 403 from `me` means the
// grant itself is gone. A failure that is not retryable (a run whose output
// already streamed) still refreshes the coordinates, so the next command
// works, but is reported rather than run again.
async function withRecovery(config, action) {
  try {
    return await action(config);
  } catch (error) {
    if (!(error instanceof HelperError) || error.exitCode === EXIT_USAGE) throw error;
    let me;
    try {
      me = JSON.parse(await gatewayJson(config, "GET", "/_agent/me"));
    } catch (meError) {
      if (meError instanceof HelperError && meError.exitCode === EXIT_REVOKED) {
        throw new HelperError("your access to this workspace was revoked", EXIT_REVOKED);
      }
      throw error;
    }
    Object.assign(config, {
      exec: me.exec,
      apps: me.apps,
      scope: me.scope,
      helper: me.helper,
      workspace: { ...config.workspace, ...me.workspace },
    });
    saveConfig(config);
    warnIfHelperDiffers(me.helper);
    if (!error.isRetryable) {
      process.stderr.write("wsvisit: refreshed the workspace's addresses; the next command will use them\n");
      throw error;
    }
    process.stderr.write("wsvisit: refreshed the workspace's addresses and retrying\n");
    return await action(config);
  }
}

// The helper the workspace publishes

function ownHelperSha256() {
  return crypto.createHash("sha256").update(fs.readFileSync(__filename)).digest("hex");
}

// A line for stderr when this file is not the helper the workspace publishes
// (another template version, or a copy altered in transit), or null when it
// is or the workspace did not say.
function helperMismatchNotice(helperInfo, ownSha256) {
  if (!helperInfo || typeof helperInfo.sha256 !== "string" || !helperInfo.sha256) return null;
  if (helperInfo.sha256 === ownSha256) return null;
  const published = `${helperInfo.sha256.slice(0, 12)}, template ${helperInfo.version || "unknown"}`;
  const where = helperInfo.source_url ? ` reinstall it from ${helperInfo.source_url}` : " reinstall it";
  return `wsvisit: this copy of the helper (${ownSha256.slice(0, 12)}) is not the one the workspace publishes (${published});${where}`;
}

function warnIfHelperDiffers(helperInfo) {
  const notice = helperMismatchNotice(helperInfo, ownHelperSha256());
  if (notice) process.stderr.write(notice + "\n");
}

// Orientation

function skillHashes(index) {
  const hashes = {};
  for (const skill of index.skills || []) hashes[skill.name] = skill.sha256;
  return hashes;
}

function describeSkillChanges(previous, current) {
  const lines = [];
  for (const name of Object.keys(current)) if (!(name in previous)) lines.push(`  new skill: ${name}`);
  for (const name of Object.keys(previous)) if (!(name in current)) lines.push(`  removed skill: ${name}`);
  for (const name of Object.keys(current)) if (name in previous && previous[name] !== current[name]) lines.push(`  changed skill: ${name}`);
  return lines;
}

async function refreshSkills(config, isForced) {
  const fetchedAt = config.skills_fetched_at ? Date.parse(config.skills_fetched_at) : 0;
  if (!isForced && Date.now() - fetchedAt < SKILLS_REFRESH_MS) return null;
  const index = JSON.parse(await gatewayJson(config, "GET", "/_agent/skills"));
  const previous = config.skill_hashes || {};
  const current = skillHashes(index);
  const changeLines = describeSkillChanges(previous, current);
  let changesCount = 0;
  if (config.skills_fetched_at) {
    const feed = JSON.parse(await gatewayJson(config, "GET", "/_agent/changes", undefined, { since: config.skills_fetched_at }));
    changesCount = (feed.commits || []).length;
  }
  config.skill_hashes = current;
  config.skills_fetched_at = new Date().toISOString();
  saveConfig(config);
  if (!isForced) {
    const banner = ["wsvisit: the skills index was refreshed (it was older than a day)."];
    if (changeLines.length) banner.push(...changeLines);
    else banner.push("  no skill changed");
    if (changesCount) banner.push(`  ${changesCount} change(s) in the workspace since you last looked; see: wsvisit changes`);
    banner.push("  read a changed skill before doing the work it covers: wsvisit skills");
    process.stderr.write(banner.join("\n") + "\n");
  }
  return index;
}

// Commands

function parseArgs(argv) {
  const options = {};
  const positional = [];
  let rest = null;
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (rest !== null) {
      rest.push(argument);
    } else if (argument === "--") {
      rest = [];
    } else if (argument.startsWith("--")) {
      const key = argument.slice(2);
      const next = argv[index + 1];
      if (next === undefined || next.startsWith("--")) {
        options[key] = true;
      } else {
        options[key] = next;
        index += 1;
      }
    } else {
      positional.push(argument);
    }
  }
  return { options, positional, rest: rest || [] };
}

async function commandJoin(positional, options) {
  const [authOrigin, code] = positional;
  if (!authOrigin || !code) throw new HelperError("usage: wsvisit join <auth-origin> <code> [--name <name>]", EXIT_USAGE);
  const origin = authOrigin.replace(/\/+$/, "");
  const domain = new URL(origin).hostname.split(".").slice(1).join(".");
  const existing = loadConfig(domain);
  const keypair = existing ? existing.keypair : generateKeypair();
  const name = options.name || existing?.name || `${os.userInfo().username}@${os.hostname()}`;
  const response = await fetchWithTimeout(
    `${origin}/_agent/join`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, public_key: keypair.publicKeyLine, name }),
    },
    REQUEST_TIMEOUT_MS,
  );
  const text = await response.text();
  if (!response.ok) {
    let message = text.slice(0, 300);
    try {
      message = JSON.parse(text).error || message;
    } catch (_error) {
      // not JSON; the raw text is the message
    }
    throw new HelperError(`join refused (${response.status}): ${message}`);
  }
  const answer = JSON.parse(text);
  const config = {
    ...answer,
    gateway_origin: origin,
    public_key_line: keypair.publicKeyLine,
    keypair,
    joined_at: new Date().toISOString(),
  };
  saveConfig(config);
  warnIfHelperDiffers(answer.helper);
  await refreshSkills(config, true);
  process.stderr.write(`wsvisit: joined ${config.workspace.display_name || domain} as ${config.name} (${config.agent_id})\n`);
  process.stdout.write(await gatewayJson(config, "GET", "/_agent/primer"));
}

// `--timeout` given without a value parses as `true`, which Number() reads as 1.
function runTimeoutSeconds(options) {
  if (options.timeout === undefined) return DEFAULT_RUN_TIMEOUT_SECONDS;
  const seconds = Number(options.timeout);
  if (options.timeout === true || !Number.isFinite(seconds) || seconds <= 0) {
    throw new HelperError("--timeout takes a positive number of seconds", EXIT_USAGE);
  }
  return Math.min(seconds, MAX_RUN_TIMEOUT_SECONDS);
}

// Same shape as `--timeout`: a bare flag reads as 1 and a word as NaN, which
// JSON would send as null (no expiry at all).
function inviteGrantLifetimeDays(options) {
  const raw = options["expires-in-days"];
  if (raw === undefined) return undefined;
  const days = Number(raw);
  if (raw === true || !Number.isFinite(days) || days <= 0) {
    throw new HelperError("--expires-in-days takes a positive number of days", EXIT_USAGE);
  }
  return days;
}

async function commandRun(config, options, rest) {
  if (!rest.length) throw new HelperError("usage: wsvisit run [--cwd <dir>] [--timeout <seconds>] -- <command...>", EXIT_USAGE);
  await refreshSkills(config, false);
  const timeoutSeconds = runTimeoutSeconds(options);
  const payload = { command: rest, timeout_seconds: timeoutSeconds };
  if (options.cwd) payload.cwd = options.cwd;
  return withRecovery(config, async (current) => {
    const { response, signed } = await execRequest(current, "/run", payload, { timeoutMs: (timeoutSeconds + 60) * 1000 });
    if (!response.ok) {
      const body = Buffer.from(await response.arrayBuffer());
      verifyResponse(response.status, response.headers, body, signed, current.exec.host_public_key, nowUnix());
      const note = clockSkewNote(headerValue(response.headers, "date"), nowUnix());
      throw new HelperError(`run refused (${response.status}): ${body.toString("utf8").slice(0, 300)}${note}`);
    }
    const outcome = await consumeRunStream(response.body, current.exec.host_public_key, signed.signatureB64);
    return outcome.exitCode;
  });
}

async function consumeRunStream(body, hostPublicKeyLine, requestSignatureB64) {
  // The trailer signs every byte before its own line, so the digest is kept
  // over whole lines as they complete and the trailer line is never fed in.
  const hash = crypto.createHash("sha256");
  const reader = body.getReader();
  let pending = Buffer.alloc(0);
  let exitCode = null;
  let trailer = null;
  const handleLine = (lineBuffer) => {
    const line = lineBuffer.toString("utf8");
    if (!line.trim()) {
      hash.update(lineBuffer);
      hash.update("\n");
      return;
    }
    const event = JSON.parse(line);
    if (event.type === "signature") {
      trailer = event;
      return;
    }
    hash.update(lineBuffer);
    hash.update("\n");
    if (event.type === "stdout") process.stdout.write(event.data || "");
    else if (event.type === "stderr") process.stderr.write(event.data || "");
    else if (event.type === "exit") exitCode = typeof event.code === "number" ? event.code : 1;
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    pending = Buffer.concat([pending, Buffer.from(value)]);
    let newline;
    while ((newline = pending.indexOf(0x0a)) >= 0) {
      handleLine(pending.subarray(0, newline));
      pending = pending.subarray(newline + 1);
    }
  }
  if (pending.length) handleLine(pending);
  try {
    verifyStreamTrailer(trailer, hash.digest(), requestSignatureB64, hostPublicKeyLine, nowUnix());
  } catch (error) {
    if (!(error instanceof HelperError)) throw error;
    // The command has run and its output is above; it must not run again.
    throw new HelperError(`${error.message} (the command ran; its output above is unverified)`, error.exitCode, false);
  }
  return { exitCode: exitCode === null ? 1 : exitCode };
}

async function commandRead(config, positional) {
  const [filePath] = positional;
  if (!filePath) throw new HelperError("usage: wsvisit read <path>", EXIT_USAGE);
  await refreshSkills(config, false);
  return withRecovery(config, async (current) => {
    const answer = await verifiedExecJson(current, "/read-file", { path: filePath });
    if (answer.status === 404 || answer.body.exists === false) {
      throw new HelperError(`no such file in the workspace: ${filePath}`, EXIT_USAGE);
    }
    process.stdout.write(Buffer.from(answer.body.content_b64 || "", "base64"));
    return 0;
  });
}

async function commandWrite(config, positional, options) {
  const [filePath] = positional;
  if (!filePath) throw new HelperError("usage: wsvisit write <path> [--from <file>] [--mode <octal>]", EXIT_USAGE);
  await refreshSkills(config, false);
  const content = options.from ? fs.readFileSync(options.from) : fs.readFileSync(0);
  const payload = { path: filePath, content_b64: content.toString("base64") };
  if (options.mode) payload.mode = String(options.mode);
  return withRecovery(config, async (current) => {
    const answer = await verifiedExecJson(current, "/write-file", payload);
    if (answer.status === 404) throw new HelperError(`could not write ${filePath}: ${answer.body.error || "not found"}`, EXIT_USAGE);
    return 0;
  });
}

async function commandPrimer(config) {
  process.stdout.write(await gatewayJson(config, "GET", "/_agent/primer", undefined, { since: config.skills_fetched_at }));
  return 0;
}

async function commandSkills(config, options) {
  const index = await refreshSkills(config, true);
  if (options.json) {
    process.stdout.write(JSON.stringify(index, null, 2) + "\n");
    return 0;
  }
  const lines = [`Skills of ${config.workspace.display_name || config.workspace.domain} (template ${index.template_ref}):`];
  for (const skill of index.skills || []) lines.push(`  ${skill.name}: ${skill.description}  [${skill.path}]`);
  lines.push("Read a skill with: wsvisit read <path>");
  process.stdout.write(lines.join("\n") + "\n");
  return 0;
}

async function commandChanges(config, options) {
  const since = options.since || config.skills_fetched_at;
  process.stdout.write(await gatewayJson(config, "GET", "/_agent/changes", undefined, { since }));
  process.stdout.write("\n");
  return 0;
}

// Apps over the share

// The origins the admission token may be sent to: the workspace's own.
function workspaceOrigins(config) {
  const origins = new Set([gatewayOrigin(config)]);
  if (config.workspace && config.workspace.auth_origin) origins.add(config.workspace.auth_origin);
  for (const app of config.apps || []) if (app.origin) origins.add(app.origin);
  return origins;
}

// `<app>/<path>` names a registered app by its name; a full URL must be on one
// of the workspace's origins, since the token goes with the request.
function resolveGetUrl(config, target) {
  if (!target) throw new HelperError("usage: wsvisit get <app>/<path> | <url>", EXIT_USAGE);
  if (/^https?:\/\//.test(target)) {
    const url = new URL(target);
    if (!workspaceOrigins(config).has(url.origin)) {
      throw new HelperError(`${url.origin} is not one of this workspace's origins, so your access is not sent there`, EXIT_USAGE);
    }
    return url;
  }
  const slash = target.indexOf("/");
  const appName = slash === -1 ? target : target.slice(0, slash);
  const routePath = slash === -1 ? "/" : target.slice(slash);
  const app = (config.apps || []).find((entry) => entry.name === appName);
  if (!app) {
    const names = (config.apps || []).map((entry) => entry.name).join(", ") || "(none)";
    throw new HelperError(`no app named ${appName} in your access; the apps are: ${names}`, EXIT_USAGE);
  }
  return new URL(routePath, app.origin);
}

// How long a 503 asks to be waited out, bounded; null when the answer is not
// one to retry.
function retryDelayMs(status, retryAfterHeader) {
  if (status !== 503) return null;
  const header = retryAfterHeader === null || retryAfterHeader === undefined ? "" : String(retryAfterHeader).trim();
  const seconds = header === "" ? NaN : Number(header);
  if (!Number.isFinite(seconds) || seconds < 0) return 1000;
  return Math.min(seconds * 1000, GET_MAX_RETRY_DELAY_MS);
}

// Redirects are followed by hand, each hop checked against `origins` before
// the request (and the bearer in it) goes there: fetch's own following would
// take it wherever Location points.
async function fetchWithinOrigins(url, init, timeoutMs, origins) {
  let current = new URL(url);
  for (let hop = 0; ; hop += 1) {
    const response = await fetchWithTimeout(current, { ...init, redirect: "manual" }, timeoutMs);
    const location = response.headers.get("location");
    if (!REDIRECT_STATUSES.has(response.status) || !location) return response;
    await response.arrayBuffer();
    if (hop >= GET_MAX_REDIRECTS) throw new HelperError(`GET ${url} redirected more than ${GET_MAX_REDIRECTS} times`);
    const next = new URL(location, current);
    if (!origins.has(next.origin)) {
      throw new HelperError(`GET ${current} redirects to ${next.origin}, which is not one of this workspace's origins, so your access is not sent there`);
    }
    current = next;
  }
}

async function fetchWaitingForTheApp(url, init, timeoutMs, origins, maxRetries = GET_MAX_RETRIES) {
  let response = await fetchWithinOrigins(url, init, timeoutMs, origins);
  for (let attempt = 0; attempt < maxRetries; attempt += 1) {
    const delay = retryDelayMs(response.status, response.headers.get("retry-after"));
    if (delay === null) break;
    await response.arrayBuffer();
    await new Promise((resolve) => setTimeout(resolve, delay));
    response = await fetchWithinOrigins(url, init, timeoutMs, origins);
  }
  return response;
}

async function commandGet(config, positional) {
  return withRecovery(config, async (current) => {
    const url = resolveGetUrl(current, positional[0]);
    const response = await fetchWaitingForTheApp(
      url,
      { method: "GET", headers: { Authorization: `Bearer ${current.token}`, Accept: "application/json, text/*;q=0.9, */*;q=0.8" } },
      REQUEST_TIMEOUT_MS,
      workspaceOrigins(current),
    );
    const body = Buffer.from(await response.arrayBuffer());
    if (response.status === 403) throw new HelperError("the workspace refused this access (revoked, or the workspace moved)", EXIT_REVOKED);
    if (!response.ok) throw new HelperError(`GET ${url} answered ${response.status}: ${body.toString("utf8").slice(0, 300)}`);
    process.stdout.write(body);
    return 0;
  });
}

async function commandMe(config) {
  const me = JSON.parse(await gatewayJson(config, "GET", "/_agent/me"));
  Object.assign(config, { exec: me.exec, apps: me.apps, scope: me.scope, workspace: { ...config.workspace, ...me.workspace } });
  saveConfig(config);
  process.stdout.write(JSON.stringify(me, null, 2) + "\n");
  return 0;
}

async function commandInvite(config, options) {
  if (config.scope !== "workspace") {
    throw new HelperError("inviting needs access to the whole workspace; this access covers named apps only", EXIT_USAGE);
  }
  const body = {};
  if (options.scope && options.scope !== "workspace") body.scope = String(options.scope).split(",").map((name) => name.trim()).filter(Boolean);
  else body.scope = "workspace";
  const expiresInDays = inviteGrantLifetimeDays(options);
  if (expiresInDays !== undefined) body.expires_in_days = expiresInDays;
  if (options["name-hint"]) body.name_hint = String(options["name-hint"]);
  const answer = JSON.parse(await gatewayJson(config, "POST", "/_agent/invites", body));
  process.stdout.write(answer.instructions);
  return 0;
}

// The usage is this file's own header comment: every `//` line after the
// shebang up to the first blank line.
function usageText() {
  const lines = [];
  for (const line of fs.readFileSync(__filename, "utf8").split("\n").slice(1)) {
    if (!line.startsWith("//")) break;
    lines.push(line.replace(/^\/\/ ?/, ""));
  }
  return lines.join("\n");
}

async function main(argv) {
  const { options, positional, rest } = parseArgs(argv);
  const [command, ...commandPositional] = positional;
  if (!command || command === "help" || options.help) {
    process.stdout.write(usageText() + "\n");
    return 0;
  }
  if (command === "join") {
    await commandJoin(commandPositional, options);
    return 0;
  }
  const config = resolveConfig(options);
  switch (command) {
    case "run":
      return commandRun(config, options, rest);
    case "read":
      return commandRead(config, commandPositional);
    case "write":
      return commandWrite(config, commandPositional, options);
    case "primer":
      return commandPrimer(config);
    case "skills":
      return commandSkills(config, options);
    case "changes":
      return commandChanges(config, options);
    case "get":
      return commandGet(config, commandPositional);
    case "me":
      return commandMe(config);
    case "invite":
      return commandInvite(config, options);
    default:
      throw new HelperError(`unknown command: ${command}`, EXIT_USAGE);
  }
}

// A write to a pipe is asynchronous on macOS, and process.exit does not wait
// for it; an empty write's callback runs once everything queued before it has
// gone out.
function flushStandardStreams() {
  return Promise.all(
    [process.stdout, process.stderr].map((stream) => new Promise((resolve) => stream.write("", resolve))),
  );
}

async function exitWith(exitCode) {
  await flushStandardStreams();
  process.exit(exitCode);
}

if (require.main === module) {
  main(process.argv.slice(2)).then(
    (exitCode) => exitWith(typeof exitCode === "number" ? exitCode : 0),
    (error) => {
      process.stderr.write(`wsvisit: ${error.message}\n`);
      return exitWith(error instanceof HelperError ? error.exitCode : 1);
    },
  );
}

module.exports = {
  HelperError,
  clockSkewNote,
  commandInvite,
  contentDigest,
  describeSkillChanges,
  fetchWithTimeout,
  fingerprintOfPublicKeyLine,
  generateKeypair,
  helperMismatchNotice,
  inviteGrantLifetimeDays,
  loadConfig,
  parseArgs,
  parseSignatureInputParams,
  publicKeyLineFromRaw,
  publicKeyObjectFromLine,
  requestSignatureBase,
  resolveGetUrl,
  retryDelayMs,
  fetchWaitingForTheApp,
  fetchWithinOrigins,
  runTimeoutSeconds,
  signRequest,
  streamTrailerBase,
  verifyResponse,
  verifyStreamTrailer,
  withRecovery,
};
