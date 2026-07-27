import { createExecutionContext, env, fetchMock, waitOnExecutionContext } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import worker from "../src/index";

const T = "20260725T000000Z";
const POOL_URL = `https://apt.example.com/snap/${T}/debian/pool/main/f/foo/foo_1.0_amd64.deb`;
const POOL_KEY = "pool/debian/pool/main/f/foo/foo_1.0_amd64.deb";
const DISTS_URL = `https://apt.example.com/snap/${T}/debian/dists/trixie/InRelease`;
const DISTS_KEY = `snap/${T}/debian/dists/trixie/InRelease`;

beforeAll(() => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
});

afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
});

async function runWorker(url: string, init?: RequestInit): Promise<Response> {
  const ctx = createExecutionContext();
  const response = await worker.fetch(new Request(url, init), env, ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

describe("path validation", () => {
  it("rejects malformed timestamps and archives", async () => {
    expect((await runWorker(`https://apt.example.com/snap/latest/debian/dists/trixie/InRelease`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/Debian!/dists/trixie/InRelease`)).status).toBe(400);
  });

  it("rejects paths that decode to traversal, backslashes, or malformed encodings", async () => {
    // %2e%2e/x is collapsed by the URL layer before the Worker runs; the
    // fully-encoded forms below contain no literal "/" so they survive URL
    // normalization and only the Worker's decoded-segment check stops them.
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/%2e%2e/x`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/%2e%2e%2fx`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/a%2f..%2fb`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/a%2f%2fb`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/a%5Cb`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/a%zzb`)).status).toBe(400);
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/pool/a%20b`)).status).toBe(400);
  });

  it("rejects unknown trees and methods", async () => {
    expect((await runWorker(`https://apt.example.com/snap/${T}/debian/other/x`)).status).toBe(400);
    expect((await runWorker(POOL_URL, { method: "POST" })).status).toBe(405);
  });
});

describe("dists", () => {
  it("serves a cut index from R2 and 404s an uncut one", async () => {
    await env.MIRROR_BUCKET.put(DISTS_KEY, "signed-index");
    const hit = await runWorker(DISTS_URL);
    expect(hit.status).toBe(200);
    expect(await hit.text()).toBe("signed-index");

    const miss = await runWorker(`https://apt.example.com/snap/${T}/debian/dists/trixie/Missing`);
    expect(miss.status).toBe(404);
  });

  it("answers HEAD with headers only", async () => {
    await env.MIRROR_BUCKET.put(DISTS_KEY, "signed-index");
    const response = await runWorker(DISTS_URL, { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Length")).toBe(String("signed-index".length));
    expect(await response.text()).toBe("");
  });
});

describe("pool", () => {
  it("decodes apt's percent-encoded '+' onto the literal-plus R2 key", async () => {
    // apt requests libjq1_1.7.1-6%2bdeb13u2_amd64.deb while cut/warm store the
    // Packages Filename verbatim (literal '+'); both must hit the same key.
    await env.MIRROR_BUCKET.put("pool/debian/pool/main/j/jq/libjq1_1.7.1-6+deb13u2_amd64.deb", "plus-deb");
    const response = await runWorker(
      `https://apt.example.com/snap/${T}/debian/pool/main/j/jq/libjq1_1.7.1-6%2bdeb13u2_amd64.deb`,
    );
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("plus-deb");
  });

  it("serves a cached pool file from R2 with immutable caching, no upstream fetch", async () => {
    await env.MIRROR_BUCKET.put(POOL_KEY, "deb-bytes");
    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("deb-bytes");
    expect(response.headers.get("Cache-Control")).toBe("public, max-age=31536000, immutable");
  });

  it("serves a range from a cached pool file", async () => {
    await env.MIRROR_BUCKET.put(POOL_KEY, "deb-bytes");
    const response = await runWorker(POOL_URL, { headers: { Range: "bytes=0-2" } });
    expect(response.status).toBe(206);
    expect(await response.text()).toBe("deb");
    expect(response.headers.get("Content-Range")).toBe("bytes 0-2/9");
  });

  it("serves a suffix range from a cached pool file", async () => {
    await env.MIRROR_BUCKET.put(POOL_KEY, "deb-bytes");
    const response = await runWorker(POOL_URL, { headers: { Range: "bytes=-5" } });
    expect(response.status).toBe(206);
    expect(await response.text()).toBe("bytes");
    expect(response.headers.get("Content-Range")).toBe("bytes 4-8/9");
    expect(response.headers.get("Content-Length")).toBe("5");
  });

  it("reads through the live archive on a miss and stores the file in R2", async () => {
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(200, "live-deb", { headers: { "content-length": "8" } });

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("live-deb");

    const stored = await env.MIRROR_BUCKET.get(POOL_KEY);
    expect(stored).not.toBeNull();
    expect(await stored!.text()).toBe("live-deb");
  });

  it("falls back to snapshot.debian.org at T for superseded files", async () => {
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(404, "gone");
    fetchMock
      .get("https://snapshot.debian.org")
      .intercept({ path: `/archive/debian/${T}/pool/main/f/foo/foo_1.0_amd64.deb` })
      .reply(200, "old-deb", { headers: { "content-length": "7" } });

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("old-deb");
  });

  it("retries upstream 5xx responses before succeeding", async () => {
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(503, "throttled");
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(200, "live-deb", { headers: { "content-length": "8" } });

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("live-deb");
  });

  it("502s when the upstream keeps failing, instead of throwing or 404ing", async () => {
    // One 503 per retry attempt; the file may well exist upstream, so a
    // throttled upstream must surface as 502, never as a missing file.
    for (let attempt = 0; attempt < 3; attempt++) {
      fetchMock
        .get("https://deb.debian.org")
        .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
        .reply(503, "throttled");
    }

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(502);
    expect(await env.MIRROR_BUCKET.get(POOL_KEY)).toBeNull();
  });

  it("404s when the file exists on no upstream", async () => {
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(404, "gone");
    fetchMock
      .get("https://snapshot.debian.org")
      .intercept({ path: `/archive/debian/${T}/pool/main/f/foo/foo_1.0_amd64.deb` })
      .reply(404, "gone");

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(404);
  });

  it("keeps serving when the R2 store fails (best-effort write)", async () => {
    // A lying content-length makes the FixedLengthStream store fail while the
    // client still receives the full body.
    fetchMock
      .get("https://deb.debian.org")
      .intercept({ path: "/debian/pool/main/f/foo/foo_1.0_amd64.deb" })
      .reply(200, "live-deb", { headers: { "content-length": "999" } });

    const response = await runWorker(POOL_URL);
    expect(response.status).toBe(200);
    expect(await response.text()).toBe("live-deb");
    expect(await env.MIRROR_BUCKET.get(POOL_KEY)).toBeNull();
  });
});
