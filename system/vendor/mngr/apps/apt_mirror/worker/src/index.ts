// Snapshot-pinned apt mirror: serves frozen index sets and a shared
// read-through pool cache from R2.
//
// Routes (public, unauthenticated, GET/HEAD only):
//   GET /snap/<T>/<archive>/dists/<subpath>  frozen index files, R2 only
//   GET /snap/<T>/<archive>/pool/<subpath>   shared pool cache, read-through
//
// Pool files are version-unique and immutable, so one cache is correct for
// every T; on a miss the Worker streams the file from the live archive (then
// snapshot.debian.org at T for superseded files) to the client while storing
// it in R2 in the background. Cache writes are strictly best-effort: a failed
// write only means the next request re-fetches. The cut/warm/verify admin
// side is the Python CLI in the parent directory, writing to the same bucket.

export interface Env {
  MIRROR_BUCKET: R2Bucket;
  UPSTREAM_BASE_BY_ARCHIVE: Record<string, string>;
  SNAPSHOT_BASE: string;
}

// apt treats bodies as opaque bytes and verifies them against the signed
// indexes itself.
const MEDIA_TYPE = "application/octet-stream";

// Pool objects are immutable by construction (version-unique paths), so
// long-lived caching is always correct.
const POOL_CACHE_CONTROL = "public, max-age=31536000, immutable";

const TIMESTAMP_RE = /^\d{8}T\d{6}Z$/;
const ARCHIVE_RE = /^[a-z][a-z0-9-]*$/;
// Every character that legitimately appears in Debian dists/pool paths
// (package names, versions incl. "+"/"~", by-hash hex, index filenames).
const SUBPATH_CHARSET_RE = /^[A-Za-z0-9._+~/-]+$/;

const UPSTREAM_ATTEMPTS = 3;
const UPSTREAM_RETRY_DELAY_MS = 250;

interface MirrorPath {
  timestamp: string;
  archive: string;
  tree: "dists" | "pool";
  subpath: string;
}

// Parse and validate /snap/<T>/<archive>/<dists|pool>/<subpath>; null means 400.
function parseMirrorPath(pathname: string): MirrorPath | null {
  const match = pathname.match(/^\/snap\/([^/]+)\/([^/]+)\/(dists|pool)\/(.+)$/);
  if (match === null) {
    return null;
  }
  const [, timestamp, archive, tree, rawSubpath] = match;
  if (!TIMESTAMP_RE.test(timestamp) || !ARCHIVE_RE.test(archive)) {
    return null;
  }
  // Decode percent-encoding once, then validate and key on the DECODED form:
  // apt percent-encodes "+" in pool paths (libjq1_1.7.1-6%2bdeb13u2_amd64.deb)
  // while the cut/warm CLI stores R2 keys with the literal "+" from the
  // Packages Filename field, so both spellings must canonicalize to one key.
  let subpath: string;
  try {
    subpath = decodeURIComponent(rawSubpath);
  } catch {
    return null;
  }
  // After decoding, only Debian archive path characters may remain -- this
  // rejects backslashes, spaces, control characters, and any leftover "%"
  // ambiguity, so no two surviving requests alias different R2 keys.
  if (!SUBPATH_CHARSET_RE.test(subpath)) {
    return null;
  }
  const segments = subpath.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return null;
  }
  return { timestamp, archive, tree: tree as "dists" | "pool", subpath };
}

function headersForPool(): Record<string, string> {
  return { "Content-Type": MEDIA_TYPE, "Cache-Control": POOL_CACHE_CONTROL };
}

// Build a 200/206 response from an R2 object, honoring a parsed range.
function r2ObjectResponse(
  object: R2ObjectBody,
  extraHeaders: Record<string, string>,
  isHead: boolean,
  isRanged: boolean,
): Response {
  const headers = new Headers(extraHeaders);
  headers.set("Accept-Ranges", "bytes");
  headers.set("ETag", object.httpEtag);
  const range = object.range;
  if (isRanged && range !== undefined) {
    // R2Range has three shapes: {offset, length?}, {offset?, length}, and
    // {suffix} (a `bytes=-N` request); all must get a correct 206.
    const isSuffix = "suffix" in range;
    const length = isSuffix ? Math.min(range.suffix, object.size) : (range.length ?? object.size - (range.offset ?? 0));
    const offset = isSuffix ? object.size - length : (range.offset ?? 0);
    headers.set("Content-Range", `bytes ${offset}-${offset + length - 1}/${object.size}`);
    headers.set("Content-Length", String(length));
    return new Response(isHead ? null : object.body, { status: 206, headers });
  }
  headers.set("Content-Length", String(object.size));
  return new Response(isHead ? null : object.body, { status: 200, headers });
}

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

// Fetch an upstream URL with bounded retries on 5xx/429/network errors.
// Returns null on a definitive 404 (so callers fall through to the next
// upstream) and throws after exhausting retries: a throttled upstream must
// not masquerade as a missing file.
async function fetchUpstreamWithRetries(url: string, method: "GET" | "HEAD"): Promise<Response | null> {
  let lastError: unknown = null;
  for (let attempt = 0; attempt < UPSTREAM_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      await sleep(UPSTREAM_RETRY_DELAY_MS * attempt);
    }
    let response: Response;
    try {
      response = await fetch(url, { method, redirect: "follow" });
    } catch (error) {
      lastError = error;
      continue;
    }
    if (response.status === 404) {
      return null;
    }
    if (response.status >= 500 || response.status === 429) {
      lastError = new Error(`Upstream ${url} returned ${response.status}`);
      await response.body?.cancel();
      continue;
    }
    if (!response.ok) {
      throw new Error(`Upstream ${url} returned ${response.status}`);
    }
    return response;
  }
  throw lastError instanceof Error ? lastError : new Error(`Upstream ${url} failed`);
}

// The ordered upstream URLs for one pool file: live archive first (fast,
// unthrottled), then snapshot.debian.org at T for superseded files.
function poolUpstreamUrls(env: Env, path: MirrorPath): string[] {
  const urls: string[] = [];
  const liveBase = env.UPSTREAM_BASE_BY_ARCHIVE[path.archive];
  if (liveBase !== undefined) {
    urls.push(`${liveBase}/pool/${path.subpath}`);
  }
  urls.push(`${env.SNAPSHOT_BASE}/${path.archive}/${path.timestamp}/pool/${path.subpath}`);
  return urls;
}

// Pump a readable into a writable chunk by chunk. Replaces pipeTo/pipeThrough,
// which workerd does not implement between two TransformStream ends (the tee'd
// upstream branch and FixedLengthStream's writable are both such ends).
async function pumpStream(source: ReadableStream<Uint8Array>, destination: WritableStream<Uint8Array>): Promise<void> {
  const reader = source.getReader();
  const writer = destination.getWriter();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      await writer.write(value);
    }
    await writer.close();
  } catch (error) {
    await writer.abort(error).catch(() => undefined);
    throw error;
  }
}

// Store an upstream response body into R2, streaming when the length is known
// and buffering otherwise. Any failure is swallowed after logging: the cache
// write is strictly best-effort.
async function storePoolBody(env: Env, key: string, body: ReadableStream, contentLength: string | null): Promise<void> {
  try {
    if (contentLength !== null) {
      // R2 put needs a known-length stream; FixedLengthStream stamps the
      // upstream Content-Length onto the tee'd branch.
      const { readable, writable } = new FixedLengthStream(Number(contentLength));
      const pumpPromise = pumpStream(body, writable);
      await Promise.all([
        env.MIRROR_BUCKET.put(key, readable, { httpMetadata: { contentType: MEDIA_TYPE } }),
        pumpPromise,
      ]);
    } else {
      const buffered = await new Response(body).arrayBuffer();
      await env.MIRROR_BUCKET.put(key, buffered, { httpMetadata: { contentType: MEDIA_TYPE } });
    }
  } catch (error) {
    console.warn(`Best-effort pool cache write failed for ${key}: ${String(error)}`);
  }
}

async function servePool(request: Request, env: Env, ctx: ExecutionContext, path: MirrorPath): Promise<Response> {
  const isHead = request.method === "HEAD";
  const key = `pool/${path.archive}/pool/${path.subpath}`;

  // Edge cache first: pool files are immutable, so a cached response is
  // always correct (the Cache API serves ranges from cached full bodies).
  const cache = caches.default;
  const cached = await cache.match(request);
  if (cached !== undefined) {
    return cached;
  }

  // R2 next, honoring Range (apt resumes interrupted downloads). The range
  // option is only passed when the client actually sent a Range header --
  // passing headers without one makes some runtimes treat the get as ranged.
  const isRanged = request.headers.has("range");
  const object = await env.MIRROR_BUCKET.get(key, isRanged ? { range: request.headers } : undefined);
  if (object !== null) {
    const response = r2ObjectResponse(object, headersForPool(), isHead, isRanged);
    if (!isHead && response.status === 200) {
      ctx.waitUntil(
        cache.put(request, response.clone()).catch((error) => {
          console.warn(`Best-effort edge cache write failed for ${key}: ${String(error)}`);
        }),
      );
    }
    return response;
  }

  // Cold miss: read through the upstreams, streaming to the client while the
  // R2 store happens in the background. A ranged request gets the full 200
  // body (apt handles that); HEAD mirrors the upstream headers without
  // caching anything.
  for (const url of poolUpstreamUrls(env, path)) {
    let upstream: Response | null;
    try {
      upstream = await fetchUpstreamWithRetries(url, isHead ? "HEAD" : "GET");
    } catch (error) {
      // A failed upstream must answer 502, not 404 (the file may well exist)
      // and not an unhandled Worker exception (apt sees a clean status).
      console.warn(`Upstream fetch failed for ${key}: ${String(error)}`);
      return new Response(`Upstream fetch failed: ${url}`, { status: 502 });
    }
    if (upstream === null) {
      continue;
    }
    const headers = new Headers(headersForPool());
    const contentLength = upstream.headers.get("content-length");
    if (contentLength !== null) {
      headers.set("Content-Length", contentLength);
    }
    if (isHead || upstream.body === null) {
      return new Response(null, { status: 200, headers });
    }
    const [clientBody, storeBody] = upstream.body.tee();
    ctx.waitUntil(storePoolBody(env, key, storeBody, contentLength));
    return new Response(clientBody, { status: 200, headers });
  }
  return new Response(`Object not found in mirror or upstream: ${key}`, { status: 404 });
}

async function serveDists(request: Request, env: Env, path: MirrorPath): Promise<Response> {
  const isHead = request.method === "HEAD";
  const key = `snap/${path.timestamp}/${path.archive}/dists/${path.subpath}`;
  if (isHead) {
    const head = await env.MIRROR_BUCKET.head(key);
    if (head === null) {
      return new Response(null, { status: 404 });
    }
    const headers = new Headers({ "Content-Type": MEDIA_TYPE, "Content-Length": String(head.size) });
    return new Response(null, { status: 200, headers });
  }
  const isRanged = request.headers.has("range");
  const object = await env.MIRROR_BUCKET.get(key, isRanged ? { range: request.headers } : undefined);
  if (object === null) {
    return new Response(`Not cut: ${key}`, { status: 404 });
  }
  return r2ObjectResponse(object, { "Content-Type": MEDIA_TYPE }, false, isRanged);
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    const path = parseMirrorPath(new URL(request.url).pathname);
    if (path === null) {
      return new Response("Bad mirror path", { status: 400 });
    }
    if (path.tree === "dists") {
      return serveDists(request, env, path);
    }
    return servePool(request, env, ctx, path);
  },
} satisfies ExportedHandler<Env>;
