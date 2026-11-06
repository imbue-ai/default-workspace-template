// The helper's signing profile against the owner-exec repo's shared vectors
// (vendored beside it), plus its own contracts: argument parsing and the
// skills-diff banner.
"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const helper = require("./wsvisit.cjs");

const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, "owner_exec_vectors.json"), "utf8"));

function headerValue(headers, name) {
  const wanted = name.toLowerCase();
  for (const [key, value] of Object.entries(headers)) if (key.toLowerCase() === wanted) return value;
  return "";
}

// Rebuilds a request's signature base from its headers with the helper's own
// builder and checks the vector's signature with the vector's key: the base
// construction is what the daemon will verify, so it must match byte for byte.
function requestSignatureVerifies(vector) {
  const headers = vector.headers;
  const { params } = helper.parseSignatureInputParams(headerValue(headers, "signature-input"));
  const fullParams = headerValue(headers, "signature-input").slice("sig1=".length);
  const body = Buffer.from(vector.body_b64, "base64");
  const base = helper.requestSignatureBase(
    vector.method,
    new URL(vector.url).pathname,
    headerValue(headers, "content-digest"),
    headerValue(headers, "x-exec-audience"),
    headerValue(headers, "x-exec-public-key"),
    fullParams,
  );
  const signatureB64 = /^sig1=:(.+):$/.exec(headerValue(headers, "signature"))[1];
  const publicKey = helper.publicKeyObjectFromLine(headerValue(headers, "x-exec-public-key"));
  const isSignatureValid = crypto.verify(null, Buffer.from(base, "utf8"), publicKey, Buffer.from(signatureB64, "base64"));
  const isDigestValid = headerValue(headers, "content-digest") === helper.contentDigest(body);
  const isInWindow = Math.abs(vector.verify_at - params.created) <= 60 && vector.verify_at < params.expires;
  return { isSignatureValid, isDigestValid, isInWindow };
}

test("request vectors: the helper's signature base matches the profile", () => {
  const byName = Object.fromEntries(vectors.requests.map((vector) => [vector.name, vector]));
  assert.deepEqual(requestSignatureVerifies(byName["run-valid"]), { isSignatureValid: true, isDigestValid: true, isInWindow: true });
  assert.deepEqual(requestSignatureVerifies(byName["read-file-valid"]), { isSignatureValid: true, isDigestValid: true, isInWindow: true });
  assert.equal(requestSignatureVerifies(byName["run-tampered-body"]).isDigestValid, false);
  assert.equal(requestSignatureVerifies(byName["run-expired"]).isInWindow, false);
});

test("fingerprints match the profile's keyid", () => {
  const vector = vectors.requests[0];
  const { params } = helper.parseSignatureInputParams(vector.headers["Signature-Input"]);
  assert.equal(helper.fingerprintOfPublicKeyLine(vector.headers["X-Exec-Public-Key"]), params.keyid);
  for (const response of vectors.responses) assert.equal(helper.fingerprintOfPublicKeyLine(response.host_key_line), response.host_key_id);
});

test("a freshly generated keypair signs a request the vector key's rules accept", () => {
  const keypair = helper.generateKeypair();
  assert.match(keypair.publicKeyLine, /^ssh-ed25519 [A-Za-z0-9+/=]+$/);
  const body = Buffer.from('{"command":["echo","hi"]}');
  const signed = helper.signRequest(keypair, "POST", "/run", body, "container:host-abcd", 1786928267, "nonce-0123456789abcdef");
  const vectorLike = {
    name: "fresh",
    method: "POST",
    url: "https://owner-exec-x.example.com/run",
    body_b64: body.toString("base64"),
    verify_at: 1786928267,
    headers: signed.headers,
  };
  assert.deepEqual(requestSignatureVerifies(vectorLike), { isSignatureValid: true, isDigestValid: true, isInWindow: true });
  assert.equal(signed.headers["Signature-Input"].includes('tag="imbue-owner-exec"'), true);
});

test("response vectors: valid verifies, wrong-request fails closed", () => {
  for (const vector of vectors.responses) {
    const signedRequest = {
      method: vector.request_method,
      path: new URL(vector.request_url).pathname,
      signatureB64: /^sig1=:(.+):$/.exec(vector.request_headers.Signature)[1],
    };
    const body = Buffer.from(vector.body_b64, "base64");
    const verify = () => helper.verifyResponse(vector.status_code, vector.headers, body, signedRequest, vector.host_key_line, vector.verify_at);
    if (vector.expect_valid) assert.doesNotThrow(verify, vector.name);
    else assert.throws(verify, helper.HelperError, vector.name);
  }
});

test("stream vectors: valid verifies, tampered fails closed", () => {
  for (const vector of vectors.streams) {
    const streamSha256 = crypto.createHash("sha256").update(Buffer.from(vector.stream_bytes_b64, "base64")).digest();
    const trailer = { type: "signature", created: vector.created, keyid: vector.host_key_id, tag: "imbue-owner-exec-stream", signature: vector.signature };
    const verify = () => helper.verifyStreamTrailer(trailer, streamSha256, vector.request_signature, vector.host_key_line, vector.verify_at);
    if (vector.expect_valid) assert.doesNotThrow(verify, vector.name);
    else assert.throws(verify, helper.HelperError, vector.name);
  }
});

test("a response signed by another host key is refused", () => {
  const vector = vectors.responses.find((entry) => entry.expect_valid);
  const other = helper.generateKeypair();
  const signedRequest = { method: vector.request_method, path: "/run", signatureB64: /^sig1=:(.+):$/.exec(vector.request_headers.Signature)[1] };
  assert.throws(
    () => helper.verifyResponse(vector.status_code, vector.headers, Buffer.from(vector.body_b64, "base64"), signedRequest, other.publicKeyLine, vector.verify_at),
    /unexpected host key/,
  );
});

test("parseArgs splits options, positionals, and the command after --", () => {
  const parsed = helper.parseArgs(["run", "--cwd", "/tmp", "--timeout", "30", "--", "ls", "--all"]);
  assert.deepEqual(parsed, { options: { cwd: "/tmp", timeout: "30" }, positional: ["run"], rest: ["ls", "--all"] });
  assert.deepEqual(helper.parseArgs(["skills", "--json"]).options, { json: true });
});

test("the run timeout is the default, a positive number of seconds capped at an hour, or a usage error", () => {
  assert.equal(helper.runTimeoutSeconds({}), 600);
  assert.equal(helper.runTimeoutSeconds({ timeout: "30" }), 30);
  assert.equal(helper.runTimeoutSeconds({ timeout: "7200" }), 3600);
  for (const timeout of [true, "5m", "0", "-1"]) {
    assert.throws(() => helper.runTimeoutSeconds({ timeout }), (error) => error instanceof helper.HelperError && error.exitCode === 64);
  }
});

test("the invite's grant lifetime is absent, a positive number of days, or a usage error", () => {
  assert.equal(helper.inviteGrantLifetimeDays({}), undefined);
  assert.equal(helper.inviteGrantLifetimeDays({ "expires-in-days": "7" }), 7);
  assert.equal(helper.inviteGrantLifetimeDays({ "expires-in-days": "0.5" }), 0.5);
  for (const days of [true, "a week", "0", "-3"]) {
    assert.throws(() => helper.inviteGrantLifetimeDays({ "expires-in-days": days }), (error) => error instanceof helper.HelperError && error.exitCode === 64);
  }
});

test("describeSkillChanges names new, removed, and changed skills", () => {
  const lines = helper.describeSkillChanges({ a: "1", b: "2", c: "3" }, { a: "1", b: "9", d: "4" });
  assert.deepEqual(lines, ["  new skill: d", "  removed skill: c", "  changed skill: b"]);
  assert.deepEqual(helper.describeSkillChanges({}, {}), []);
});

test("the helper says when it is not the one the workspace publishes, and stays quiet when it is or nothing was said", () => {
  const own = "a".repeat(64);
  const published = { sha256: "b".repeat(64), version: "v0.3.0", source_url: "https://example.com/wsvisit.cjs" };
  const notice = helper.helperMismatchNotice(published, own);
  assert.match(notice, /aaaaaaaaaaaa.*not the one the workspace publishes \(bbbbbbbbbbbb, template v0\.3\.0\)/);
  assert.match(notice, /reinstall it from https:\/\/example\.com\/wsvisit\.cjs/);
  assert.equal(helper.helperMismatchNotice({ ...published, sha256: own }, own), null);
  assert.equal(helper.helperMismatchNotice(undefined, own), null);
  assert.equal(helper.helperMismatchNotice({ version: "v0.3.0" }, own), null);
});

test("get names an app by its name or takes a URL on one of the workspace's origins, and refuses any other", () => {
  const config = {
    token: "t",
    gateway_origin: "https://auth-x.alpha.example.com",
    workspace: { auth_origin: "https://auth-x.alpha.example.com" },
    apps: [{ name: "files", origin: "https://files-f1.alpha.example.com" }],
  };
  assert.equal(String(helper.resolveGetUrl(config, "files/__files/api/schema")), "https://files-f1.alpha.example.com/__files/api/schema");
  assert.equal(String(helper.resolveGetUrl(config, "files")), "https://files-f1.alpha.example.com/");
  assert.equal(String(helper.resolveGetUrl(config, "https://files-f1.alpha.example.com/x?json")), "https://files-f1.alpha.example.com/x?json");
  assert.equal(String(helper.resolveGetUrl(config, "https://auth-x.alpha.example.com/_agent/me")), "https://auth-x.alpha.example.com/_agent/me");
  assert.throws(() => helper.resolveGetUrl(config, "https://evil.example.com/"), /not one of this workspace's origins/);
  assert.throws(() => helper.resolveGetUrl(config, "chat/api/x"), /no app named chat.*files/);
  assert.throws(() => helper.resolveGetUrl(config, ""), /usage: wsvisit get/);
});

test("a 503 is waited out for as long as Retry-After asks, bounded, and nothing else is retried", () => {
  assert.equal(helper.retryDelayMs(503, "2"), 2000);
  assert.equal(helper.retryDelayMs(503, "600"), 5000);
  assert.equal(helper.retryDelayMs(503, null), 1000);
  assert.equal(helper.retryDelayMs(503, "soon"), 1000);
  assert.equal(helper.retryDelayMs(200, "2"), null);
  assert.equal(helper.retryDelayMs(502, "2"), null);
  assert.equal(helper.retryDelayMs(404, null), null);
});

test("get waits for an app that answers 503 while it starts, then returns its answer", async () => {
  const http = require("node:http");
  let calls = 0;
  const server = http.createServer((request, response) => {
    calls += 1;
    if (calls < 3) {
      response.writeHead(503, { "Retry-After": "0", "Content-Type": "text/html" });
      response.end("<p>starting</p>");
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ seen: request.headers.authorization }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const url = `http://127.0.0.1:${server.address().port}/api/schema`;
    const origins = new Set([`http://127.0.0.1:${server.address().port}`]);
    const response = await helper.fetchWaitingForTheApp(url, { headers: { Authorization: "Bearer t" } }, 5000, origins);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { seen: "Bearer t" });
    assert.equal(calls, 3);
  } finally {
    server.close();
  }
});

test("get gives up on an app that keeps answering 503 after the bounded retries", async () => {
  const http = require("node:http");
  let calls = 0;
  const server = http.createServer((request, response) => {
    calls += 1;
    response.writeHead(503, { "Retry-After": "0" });
    response.end("still starting");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const url = `http://127.0.0.1:${server.address().port}/api/schema`;
    const origins = new Set([`http://127.0.0.1:${server.address().port}`]);
    assert.equal((await helper.fetchWaitingForTheApp(url, { headers: {} }, 5000, origins, 0)).status, 503);
    assert.equal(calls, 1);
    assert.equal((await helper.fetchWaitingForTheApp(url, { headers: {} }, 5000, origins, 2)).status, 503);
    assert.equal(calls, 4);
  } finally {
    server.close();
  }
});

test("get follows a redirect within the workspace's origins and refuses one that leaves them, before the bearer is sent", async () => {
  const http = require("node:http");
  const elsewhereSaw = [];
  const elsewhere = http.createServer((request, response) => {
    elsewhereSaw.push(request.headers.authorization || "(no bearer)");
    response.writeHead(200);
    response.end("elsewhere");
  });
  await new Promise((resolve) => elsewhere.listen(0, "127.0.0.1", resolve));
  const elsewhereOrigin = `http://127.0.0.1:${elsewhere.address().port}`;
  const app = http.createServer((request, response) => {
    if (request.url === "/old") {
      response.writeHead(302, { Location: "/new" });
    } else if (request.url === "/away") {
      response.writeHead(302, { Location: `${elsewhereOrigin}/landing` });
    } else if (request.url === "/loop") {
      response.writeHead(302, { Location: "/loop" });
    } else {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ path: request.url, seen: request.headers.authorization }));
      return;
    }
    response.end();
  });
  await new Promise((resolve) => app.listen(0, "127.0.0.1", resolve));
  const appOrigin = `http://127.0.0.1:${app.address().port}`;
  const origins = new Set([appOrigin]);
  const init = { headers: { Authorization: "Bearer t" } };
  try {
    const followed = await helper.fetchWithinOrigins(`${appOrigin}/old`, init, 5000, origins);
    assert.equal(followed.status, 200);
    assert.deepEqual(await followed.json(), { path: "/new", seen: "Bearer t" });
    await assert.rejects(helper.fetchWithinOrigins(`${appOrigin}/away`, init, 5000, origins), /not one of this workspace's origins/);
    assert.deepEqual(elsewhereSaw, []);
    await assert.rejects(helper.fetchWithinOrigins(`${appOrigin}/loop`, init, 5000, origins), /redirected more than/);
  } finally {
    app.close();
    elsewhere.close();
  }
});

test("a joined workspace's state is refused unless its directory is private", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wsvisit-state-"));
  process.env.WSVISIT_STATE_DIR = root;
  try {
    const dir = path.join(root, "alpha.example.com");
    fs.mkdirSync(dir, { mode: 0o755 });
    fs.writeFileSync(path.join(dir, "config.json"), JSON.stringify({ public_key_line: "ssh-ed25519 AAAA", token: "t" }));
    fs.writeFileSync(path.join(dir, "key.pem"), "pem");
    assert.throws(() => helper.loadConfig("alpha.example.com"), /expected 700/);
    fs.chmodSync(dir, 0o700);
    assert.equal(helper.loadConfig("alpha.example.com").token, "t");
    assert.equal(helper.loadConfig("other.example.com"), null);
  } finally {
    delete process.env.WSVISIT_STATE_DIR;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("an app-scope agent is told it cannot invite, not that it was revoked", async () => {
  await assert.rejects(helper.commandInvite({ scope: ["chat"], token: "t", workspace: { auth_origin: "https://auth.example.com" } }, {}), (error) => {
    assert.ok(error instanceof helper.HelperError);
    assert.equal(error.exitCode, 64);
    assert.match(error.message, /whole workspace/);
    return true;
  });
});

test("an unreachable workspace is reported with the publish hint", async () => {
  const originalFetch = global.fetch;
  global.fetch = async () => {
    throw new Error("fetch failed");
  };
  try {
    await assert.rejects(helper.fetchWithTimeout("https://auth.alpha.example.com/_agent/me", {}, 1000), (error) => {
      assert.ok(error instanceof helper.HelperError);
      assert.match(error.message, /could not reach https:\/\/auth\.alpha\.example\.com\/_agent\/me: fetch failed/);
      assert.match(error.message, /may not be published right now/);
      return true;
    });
  } finally {
    global.fetch = originalFetch;
  }
});

test("a refusal from a workspace whose clock is off names the skew", () => {
  const now = Date.parse("2026-10-15T18:00:00Z") / 1000;
  assert.equal(helper.clockSkewNote("Thu, 15 Oct 2026 18:00:30 GMT", now), "");
  assert.equal(helper.clockSkewNote("", now), "");
  assert.equal(helper.clockSkewNote("not a date", now), "");
  assert.match(helper.clockSkewNote("Thu, 15 Oct 2026 18:05:00 GMT", now), /300s ahead of/);
  assert.match(helper.clockSkewNote("Thu, 15 Oct 2026 17:50:00 GMT", now), /600s behind/);
});

test("the self-heal retries a retryable failure once and never re-runs a command that already ran", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wsvisit-heal-"));
  process.env.WSVISIT_STATE_DIR = root;
  const originalFetch = global.fetch;
  const me = {
    exec: { origin: "https://exec-new.example.com", audience: "container:host-1", host_public_key: "ssh-ed25519 AAAA" },
    apps: [],
    scope: "workspace",
    workspace: { domain: "alpha.example.com" },
  };
  global.fetch = async () => new Response(JSON.stringify(me), { status: 200 });
  try {
    const config = {
      token: "t",
      gateway_origin: "https://auth.alpha.example.com",
      workspace: { domain: "alpha.example.com", auth_origin: "https://auth.alpha.example.com" },
      exec: { origin: "https://exec-old.example.com" },
      keypair: { privateKeyPem: "pem", publicKeyLine: "ssh-ed25519 AAAA" },
    };
    let attempts = 0;
    const answer = await helper.withRecovery(config, async (current) => {
      attempts += 1;
      if (attempts === 1) throw new helper.HelperError("stale host key", 70);
      return current.exec.origin;
    });
    assert.equal(attempts, 2);
    assert.equal(answer, "https://exec-new.example.com");

    config.exec = { origin: "https://exec-old.example.com" };
    let runs = 0;
    await assert.rejects(
      helper.withRecovery(config, async () => {
        runs += 1;
        throw new helper.HelperError("stream signature does not verify", 70, false);
      }),
      /stream signature does not verify/,
    );
    assert.equal(runs, 1);
    assert.equal(config.exec.origin, "https://exec-new.example.com");
  } finally {
    global.fetch = originalFetch;
    delete process.env.WSVISIT_STATE_DIR;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
