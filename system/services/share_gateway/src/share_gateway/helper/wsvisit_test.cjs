// The helper's own contracts: argument parsing, the requests it sends for a
// command and a file, how it reads a command's stream, the skills-diff banner,
// and the self-heal.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { Readable } = require("node:stream");
const { test } = require("node:test");

const helper = require("./wsvisit.cjs");

// A writable that keeps what it is given, standing in for stdout or stderr.
function sink() {
  const written = [];
  return { written, write: (text) => written.push(String(text)) };
}

function streamOf(...chunks) {
  return Readable.from(chunks.map((chunk) => Buffer.from(chunk)));
}

// A loopback server that records each request and answers with `respond(request, body)`:
// a status and either a whole `body` or the `chunks` of a streamed one.
async function withServer(respond, run) {
  const seen = [];
  const server = http.createServer((request, response) => {
    const parts = [];
    request.on("data", (part) => parts.push(part));
    request.on("end", () => {
      const body = Buffer.concat(parts).toString("utf8");
      seen.push({ method: request.method, url: request.url, authorization: request.headers.authorization, body });
      const answer = respond(request, body);
      response.writeHead(answer.status, { "Content-Type": answer.type || "application/json" });
      for (const chunk of answer.chunks || []) response.write(chunk);
      response.end(answer.body);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    await run(`http://127.0.0.1:${server.address().port}`, seen);
  } finally {
    server.close();
  }
}

function workspaceConfig(origin, scope = "workspace") {
  return { token: "tok-123", scope, gateway_origin: origin, workspace: { auth_origin: "https://auth.example.com" } };
}

test("a freshly generated key is an OpenSSH Ed25519 line, and each one differs", () => {
  const first = helper.generateKeypair();
  assert.match(first.publicKeyLine, /^ssh-ed25519 [A-Za-z0-9+/=]+$/);
  assert.match(first.privateKeyPem, /^-----BEGIN PRIVATE KEY-----/);
  assert.notEqual(helper.generateKeypair().publicKeyLine, first.publicKeyLine);
  const wire = Buffer.from(first.publicKeyLine.split(" ")[1], "base64");
  assert.equal(wire.length, 4 + "ssh-ed25519".length + 4 + 32);
  assert.equal(wire.subarray(4, 15).toString(), "ssh-ed25519");
});

test("a file is asked of the gateway with the admission token and nothing else", async () => {
  await withServer(
    () => ({ status: 200, body: JSON.stringify({ written: true }) }),
    async (origin, seen) => {
      const answer = await helper.workspaceJson(workspaceConfig(origin), "/_agent/files/write", { path: "data/a.txt", content_b64: "aGk=" });
      assert.deepEqual(answer, { status: 200, isOk: true, body: { written: true }, detail: '{"written":true}' });
      assert.deepEqual(seen, [
        { method: "POST", url: "/_agent/files/write", authorization: "Bearer tok-123", body: '{"path":"data/a.txt","content_b64":"aGk="}' },
      ]);
    },
  );
});

test("a command is asked of the gateway with the admission token, and its stream is read as it is sent", async () => {
  await withServer(
    () => ({ status: 200, type: "application/x-ndjson", chunks: ['{"type":"stdout","data":"hi\\n"}\n', '{"type":"exit","code":0}\n'] }),
    async (origin, seen) => {
      const stdout = sink();
      const stream = await helper.openRunStream(workspaceConfig(origin), { command: ["echo", "hi"], timeout_seconds: 5 }, 5000);
      assert.equal(await helper.consumeRunStream(stream, stdout, sink()), 0);
      assert.deepEqual(stdout.written, ["hi\n"]);
      assert.deepEqual(seen, [
        { method: "POST", url: "/_agent/run", authorization: "Bearer tok-123", body: '{"command":["echo","hi"],"timeout_seconds":5}' },
      ]);
    },
  );
});

test("an access to named apps is told it cannot run commands or move files before anything is sent", async () => {
  await withServer(
    () => ({ status: 200, body: "{}" }),
    async (origin, seen) => {
      const isUsageError = (error) => {
        assert.ok(error instanceof helper.HelperError);
        assert.equal(error.exitCode, 64);
        assert.match(error.message, /named apps only/);
        return true;
      };
      await assert.rejects(helper.openRunStream(workspaceConfig(origin, ["chat"]), { command: ["ls"] }, 5000), isUsageError);
      await assert.rejects(helper.workspaceJson(workspaceConfig(origin, ["chat"]), "/_agent/files/read", { path: "a" }), isUsageError);
      assert.deepEqual(seen, []);
    },
  );
});

test("a 403 from the gateway is a refused access, which the self-heal then checks", async () => {
  await withServer(
    () => ({ status: 403, body: JSON.stringify({ error: "this access token is not valid" }) }),
    async (origin) => {
      const isRefused = (error) => {
        assert.ok(error instanceof helper.HelperError);
        assert.equal(error.exitCode, 77);
        assert.equal(error.isRetryable, true);
        return true;
      };
      await assert.rejects(helper.openRunStream(workspaceConfig(origin), { command: ["ls"] }, 5000), isRefused);
      await assert.rejects(helper.workspaceJson(workspaceConfig(origin), "/_agent/files/read", { path: "a" }), isRefused);
    },
  );
});

test("a run the gateway calls wrong is a usage error that carries the gateway's words", async () => {
  await withServer(
    () => ({ status: 400, body: JSON.stringify({ error: "cwd must be a string" }) }),
    async (origin) => {
      await assert.rejects(helper.openRunStream(workspaceConfig(origin), { command: ["ls"], cwd: 5 }, 5000), (error) => {
        assert.ok(error instanceof helper.HelperError);
        assert.equal(error.exitCode, 64);
        assert.equal(error.message, "run answered 400: cwd must be a string");
        return true;
      });
    },
  );
});

test("a run that gets no answer is not retried, since the command may have started", async () => {
  const server = http.createServer((request) => request.socket.destroy());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    await assert.rejects(helper.openRunStream(workspaceConfig(origin), { command: ["ls"] }, 5000), (error) => {
      assert.ok(error instanceof helper.HelperError);
      assert.equal(error.isRetryable, false);
      assert.match(error.message, /could not reach .*\/_agent\/run: .*may not be published right now/);
      return true;
    });
  } finally {
    server.close();
  }
});

test("a run whose answer stays silent longer than it is allowed is given up on", async () => {
  const server = http.createServer(() => {});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const origin = `http://127.0.0.1:${server.address().port}`;
    await assert.rejects(helper.openRunStream(workspaceConfig(origin), { command: ["ls"] }, 50), /nothing arrived for 0 seconds/);
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test("a refusal's detail is the gateway's error, or the text of an answer that is not the gateway's", () => {
  assert.deepEqual(helper.jsonAnswer(400, JSON.stringify({ error: "path must be a non-empty string" })), {
    status: 400,
    isOk: false,
    body: { error: "path must be a non-empty string" },
    detail: "path must be a non-empty string",
  });
  assert.equal(helper.jsonAnswer(404, JSON.stringify({ exists: false })).body.exists, false);
  assert.deepEqual(helper.jsonAnswer(404, "<h1>Not Found</h1>"), { status: 404, isOk: false, body: {}, detail: "<h1>Not Found</h1>" });
  const read = helper.jsonAnswer(200, JSON.stringify({ exists: true, content_b64: "aGk=" }));
  assert.equal(read.isOk, true);
  assert.equal(read.body.content_b64, "aGk=");
  assert.deepEqual(helper.jsonAnswer(502, '"a bare string"'), { status: 502, isOk: false, body: {}, detail: '"a bare string"' });
});

test("a request the gateway calls wrong is a usage error, and any other refusal may be retried", () => {
  const wrong = helper.refusalError("write", { status: 400, detail: "mode must be octal" });
  assert.equal(wrong.exitCode, 64);
  assert.equal(wrong.message, "write answered 400: mode must be octal");
  assert.equal(helper.refusalError("write", { status: 413, detail: "the body is too large" }).exitCode, 64);
  const failed = helper.refusalError("run", { status: 502, detail: "bad gateway" });
  assert.equal(failed.exitCode, 1);
  assert.equal(failed.isRetryable, true);
});

test("a run's stream is written out as it arrives and ends with the command's exit code", async () => {
  const stdout = sink();
  const stderr = sink();
  // The second line is split across two chunks, as a network read may split it.
  const body = streamOf(
    '{"type":"stdout","data":"one\\n"}\n{"type":"std',
    'err","data":"oops\\n"}\n\n{"type":"stdout","data":"caf\u00e9\\n"}\n',
    '{"type":"exit","code":3}\n',
  );
  assert.equal(await helper.consumeRunStream(body, stdout, stderr), 3);
  assert.deepEqual(stdout.written, ["one\n", "caf\u00e9\n"]);
  assert.deepEqual(stderr.written, ["oops\n"]);
});

test("a run's stream that ends without the exit is an error the self-heal must not answer by running again", async () => {
  const stdout = sink();
  await assert.rejects(helper.consumeRunStream(streamOf('{"type":"stdout","data":"partial\\n"}\n'), stdout, sink()), (error) => {
    assert.ok(error instanceof helper.HelperError);
    assert.equal(error.isRetryable, false);
    assert.match(error.message, /ended before the command's exit was reported/);
    return true;
  });
  assert.deepEqual(stdout.written, ["partial\n"]);
});

test("a run's stream that breaks mid-way is an error that is not retried", async () => {
  const body = new Readable({
    read() {
      this.destroy(new Error("socket hang up"));
    },
  });
  await assert.rejects(helper.consumeRunStream(body, sink(), sink()), (error) => {
    assert.ok(error instanceof helper.HelperError);
    assert.equal(error.isRetryable, false);
    assert.match(error.message, /connection dropped while the command was running: socket hang up/);
    return true;
  });
});

test("a command with no exit code of its own ends as 1 and says why", () => {
  const quiet = sink();
  assert.equal(helper.runExitCode({ type: "exit", code: 0 }, quiet), 0);
  assert.equal(helper.runExitCode({ type: "exit", code: -1 }, quiet), -1);
  assert.deepEqual(quiet.written, []);
  const timedOut = sink();
  assert.equal(helper.runExitCode({ type: "exit", code: null, timed_out: true }, timedOut), 1);
  assert.deepEqual(timedOut.written, ["wsvisit: the command ran out of time and was stopped\n"]);
  const notStarted = sink();
  assert.equal(helper.runExitCode({ type: "exit", code: null, error: "No such file or directory: 'nope'" }, notStarted), 1);
  assert.deepEqual(notStarted.written, ["wsvisit: the command could not be started: No such file or directory: 'nope'\n"]);
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

test("the self-heal retries a retryable failure once and never re-runs a command that already ran", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "wsvisit-heal-"));
  process.env.WSVISIT_STATE_DIR = root;
  const originalFetch = global.fetch;
  const me = {
    apps: [{ name: "files", origin: "https://files-new.alpha.example.com" }],
    scope: "workspace",
    workspace: { domain: "alpha.example.com" },
  };
  global.fetch = async () => new Response(JSON.stringify(me), { status: 200 });
  try {
    const staleApps = [{ name: "files", origin: "https://files-old.alpha.example.com" }];
    const config = {
      token: "t",
      gateway_origin: "https://auth.alpha.example.com",
      workspace: { domain: "alpha.example.com", auth_origin: "https://auth.alpha.example.com" },
      apps: staleApps,
      keypair: { privateKeyPem: "pem", publicKeyLine: "ssh-ed25519 AAAA" },
    };
    let attempts = 0;
    const answer = await helper.withRecovery(config, async (current) => {
      attempts += 1;
      if (attempts === 1) throw new helper.HelperError("the app moved");
      return current.apps[0].origin;
    });
    assert.equal(attempts, 2);
    assert.equal(answer, "https://files-new.alpha.example.com");

    config.apps = staleApps;
    let runs = 0;
    await assert.rejects(
      helper.withRecovery(config, async () => {
        runs += 1;
        throw new helper.HelperError("the run's stream ended before the command's exit was reported", 1, false);
      }),
      /ended before the command's exit was reported/,
    );
    assert.equal(runs, 1);
    assert.equal(config.apps[0].origin, "https://files-new.alpha.example.com");

    let wrongRequests = 0;
    await assert.rejects(
      helper.withRecovery(config, async () => {
        wrongRequests += 1;
        throw helper.refusalError("read", { status: 400, detail: "could not read data: Is a directory" });
      }),
      /could not read data: Is a directory/,
    );
    assert.equal(wrongRequests, 1);
  } finally {
    global.fetch = originalFetch;
    delete process.env.WSVISIT_STATE_DIR;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
