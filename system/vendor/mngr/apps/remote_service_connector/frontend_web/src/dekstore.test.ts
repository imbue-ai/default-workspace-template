// The two-tabs-unlocking-concurrently edge cases, against a stubbed fetch,
// a fake IndexedDB, and a sessionStorage stub. Two scenarios matter:
// concurrent unlocks of one existing bundle must converge on the same DEK,
// and concurrent first-time setups must leave exactly one usable DEK -- the
// loser must never keep the key it minted (nothing stored could recover it).

import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { KeyBundleExistsError } from "./api";
import {
  type KeyBundle,
  bytesToBase64,
  generateDek,
  unwrapBundle,
  wrapDekToBundle,
} from "./crypto/secretbox";
import {
  currentDek,
  forgetDek,
  setInitialPassword,
  unlockWithPassword,
} from "./dekstore";

function stubSessionStorage(): void {
  const backing = new Map<string, string>();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => backing.get(key) ?? null,
    setItem: (key: string, value: string) => void backing.set(key, value),
    removeItem: (key: string) => void backing.delete(key),
  });
}

// A fetch stub emulating the connector's bundle endpoints, including the
// server-side create-only semantics of PUT /sync/bundle?if_absent=true.
function stubBundleServer(initial: KeyBundle | null): {
  stored: () => KeyBundle | null;
} {
  let bundle: KeyBundle | null = initial;
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    if (url.startsWith("/sync/bundle") && (init?.method ?? "GET") === "GET") {
      if (bundle === null) return new Response(null, { status: 404 });
      return new Response(JSON.stringify(bundle), { status: 200 });
    }
    if (url.startsWith("/sync/bundle") && init?.method === "PUT") {
      const incoming = JSON.parse(String(init.body)) as KeyBundle;
      if (url.includes("if_absent=true") && bundle !== null) {
        return new Response(
          JSON.stringify({ detail: { code: "bundle_exists" } }),
          { status: 409 },
        );
      }
      bundle = incoming;
      return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
    }
    throw new Error(`unexpected fetch: ${init?.method ?? "GET"} ${url}`);
  });
  return { stored: () => bundle };
}

beforeEach(() => {
  stubSessionStorage();
});

afterEach(async () => {
  await forgetDek();
  vi.unstubAllGlobals();
});

describe("concurrent unlock", () => {
  it("two tabs unlocking the same bundle converge on the same DEK", async () => {
    const dek = generateDek();
    stubBundleServer(await wrapDekToBundle(dek, "hunter22", 1));

    const [first, second] = await Promise.all([
      unlockWithPassword("hunter22", false),
      unlockWithPassword("hunter22", true),
    ]);

    expect(first).toBe("unlocked");
    expect(second).toBe("unlocked");
    const settled = currentDek();
    expect(settled).not.toBeNull();
    expect(bytesToBase64(settled as Uint8Array)).toBe(bytesToBase64(dek));
  });

  it("two tabs racing first-time setup leave exactly one recoverable DEK", async () => {
    const server = stubBundleServer(null);

    const outcomes = await Promise.allSettled([
      setInitialPassword("first-tab-pw", false),
      setInitialPassword("second-tab-pw", false),
    ]);

    // Exactly one setup won; the other surfaced the typed race signal.
    const failures = outcomes.filter(
      (outcome) => outcome.status === "rejected",
    );
    expect(failures).toHaveLength(1);
    expect(
      (failures[0] as PromiseRejectedResult).reason,
    ).toBeInstanceOf(KeyBundleExistsError);

    // The stored bundle unwraps with the winner's password, and the DEK this
    // context holds is that same recoverable DEK -- the loser's minted key
    // was discarded, not stored.
    const stored = server.stored();
    expect(stored).not.toBeNull();
    const winnerPassword =
      outcomes[0].status === "fulfilled" ? "first-tab-pw" : "second-tab-pw";
    const recovered = await unwrapBundle(stored as KeyBundle, winnerPassword);
    const held = currentDek();
    expect(held).not.toBeNull();
    expect(bytesToBase64(held as Uint8Array)).toBe(bytesToBase64(recovered));
  });

  it("a lost setup race leaves this tab locked (no DEK) so it can prompt for the winning password", async () => {
    stubBundleServer(await wrapDekToBundle(generateDek(), "winner-pw", 1));

    await expect(setInitialPassword("loser-pw", false)).rejects.toBeInstanceOf(
      KeyBundleExistsError,
    );

    expect(currentDek()).toBeNull();
  });
});
