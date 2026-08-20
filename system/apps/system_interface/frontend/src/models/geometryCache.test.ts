import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ENTRY_TTL_MS,
  MAX_CACHED_ENTRIES,
  WIDTH_BUCKET_PX,
  createGeometryCache,
  widthBucketFor,
} from "./geometryCache";
import type { RowGeometry } from "./rowGeometry";

/**
 * These run in vitest's node environment, which ships no IndexedDB, so they
 * exercise the in-memory fallback. That is the path a browser denying storage
 * takes, and it is deliberately behaviourally identical to the database path --
 * a caller cannot tell which one it got, which is the property worth locking in.
 */
function cacheWithClock(): { cache: ReturnType<typeof createGeometryCache>; advance: (ms: number) => void } {
  let clock = 1_000_000;
  return { cache: createGeometryCache(() => clock), advance: (ms: number) => (clock += ms) };
}

function row(start: number, end: number, height: number): RowGeometry {
  return { row_key: `row-${start}`, start_offset: start, end_offset: end, height };
}

/** The handlers an IndexedDB request hands its result back through. */
interface FakeRequest {
  result: unknown;
  onsuccess: (() => void) | null;
  onerror: (() => void) | null;
  onupgradeneeded: (() => void) | null;
  onblocked: (() => void) | null;
}

/** Answer a request once the caller has had a turn to attach its handlers. */
function respond(result: unknown, outcome: "success" | "error" = "success"): FakeRequest {
  const request: FakeRequest = {
    result: undefined,
    onsuccess: null,
    onerror: null,
    onupgradeneeded: null,
    onblocked: null,
  };
  queueMicrotask(() => {
    request.result = result;
    if (outcome === "success") {
      request.onsuccess?.();
    } else {
      request.onerror?.();
    }
  });
  return request;
}

/**
 * A connection that opens and hands out an object store, but refuses every
 * write -- which is what a denied quota looks like on a database that was
 * usable a moment ago. The narrowest surface the cache actually drives; there is
 * no IndexedDB in this environment, so this is the only way to reach the
 * database path.
 */
function stubIndexedDbRefusingWrites(): void {
  const store = {
    put: () => respond(undefined, "error"),
    get: () => respond(undefined),
    getAll: () => respond([]),
    delete: () => respond(undefined),
  };
  const database = {
    objectStoreNames: { contains: () => true },
    transaction: () => ({ objectStore: () => store }),
  };
  vi.stubGlobal("indexedDB", { open: () => respond(database) });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("widthBucketFor", () => {
  it("quantizes nearby widths into the same bucket", () => {
    // A scrollbar appearing or a few pixels of panel resize must not throw away
    // a conversation's geometry.
    expect(widthBucketFor(1000)).toBe(widthBucketFor(1000 + WIDTH_BUCKET_PX / 4));
  });

  it("separates widths that differ by a real layout change", () => {
    expect(widthBucketFor(400)).not.toBe(widthBucketFor(1200));
  });

  it("never returns a negative bucket", () => {
    expect(widthBucketFor(0)).toBe(0);
    expect(widthBucketFor(-50)).toBe(0);
  });
});

describe("createGeometryCache", () => {
  it("returns null for a conversation never cached", async () => {
    const { cache } = cacheWithClock();
    expect(await cache.load("agent-a", 10)).toBeNull();
  });

  it("round-trips a snapshot", async () => {
    const { cache } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100), row(10, 20, 340)] });
    const loaded = await cache.load("agent-a", 10);
    expect(loaded?.rows.map((r) => r.height)).toEqual([100, 340]);
  });

  it("keeps width buckets separate", async () => {
    // Heights are a function of width, so a different bucket must miss rather
    // than hand back measurements describing a layout that no longer exists.
    const { cache } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });
    expect(await cache.load("agent-a", 20)).toBeNull();
    expect(await cache.load("agent-a", 10)).not.toBeNull();
  });

  it("keeps conversations separate", async () => {
    const { cache } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });
    expect(await cache.load("agent-b", 10)).toBeNull();
  });

  it("overwrites an earlier snapshot for the same conversation and width", async () => {
    const { cache } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });
    await cache.save("agent-a", 10, { rows: [row(0, 10, 250)] });
    const loaded = await cache.load("agent-a", 10);
    expect(loaded?.rows).toHaveLength(1);
    expect(loaded?.rows[0].height).toBe(250);
  });

  it("treats an expired entry as absent", async () => {
    // A conversation untouched for a month is not worth the space, and its
    // rendering may well have changed since.
    const { cache, advance } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });
    advance(ENTRY_TTL_MS + 1);
    expect(await cache.load("agent-a", 10)).toBeNull();
  });

  it("keeps an entry that is just inside the TTL", async () => {
    const { cache, advance } = cacheWithClock();
    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });
    advance(ENTRY_TTL_MS);
    expect(await cache.load("agent-a", 10)).not.toBeNull();
  });

  it("evicts the least recently written entry once over the cap", async () => {
    // The bound has to hold on this path too, or a session that never gets a
    // database accumulates a row table per conversation and width for as long as
    // it lasts.
    const { cache, advance } = cacheWithClock();
    for (let i = 0; i < MAX_CACHED_ENTRIES; i++) {
      await cache.save(`agent-${i}`, 10, { rows: [row(0, 10, 100)] });
      advance(1);
    }
    // Reading does not count as use; the oldest *write* is what goes.
    expect(await cache.load("agent-0", 10)).not.toBeNull();
    await cache.save("agent-new", 10, { rows: [row(0, 10, 100)] });
    expect(await cache.load("agent-0", 10)).toBeNull();
    expect(await cache.load("agent-1", 10)).not.toBeNull();
    expect(await cache.load("agent-new", 10)).not.toBeNull();
  });

  it("keeps a write the database refuses, rather than losing it between the two tiers", async () => {
    // A denied quota shows up as a rejected write on a connection that opened
    // fine, and the whole contract here is that a caller cannot tell the
    // database apart from the fallback. Dropping it would leave the entry in
    // neither while save() still resolved, so the next visit measures again for
    // no reason a reader could see.
    stubIndexedDbRefusingWrites();
    const { cache } = cacheWithClock();

    await cache.save("agent-a", 10, { rows: [row(0, 10, 100)] });

    expect((await cache.load("agent-a", 10))?.rows[0].height).toBe(100);
  });

  it("spends one slot per width a conversation was measured at", async () => {
    // Heights are a function of width, so one chat read at several widths is
    // several entries -- which is why the cap is a bound on the database rather
    // than on how many distinct conversations survive in it.
    const { cache, advance } = cacheWithClock();
    for (let bucket = 1; bucket <= MAX_CACHED_ENTRIES; bucket++) {
      await cache.save("agent-a", bucket, { rows: [row(0, 10, 100)] });
      advance(1);
    }
    await cache.save("agent-a", MAX_CACHED_ENTRIES + 1, { rows: [row(0, 10, 100)] });

    expect(await cache.load("agent-a", 1)).toBeNull();
    expect(await cache.load("agent-a", 2)).not.toBeNull();
  });
});
