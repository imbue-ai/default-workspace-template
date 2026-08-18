import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  TOUCH_THROTTLE_MS,
  applyMemberLastUsedChange,
  fetchMemberLastUsed,
  getMemberLastUsed,
  loadMemberLastUsed,
  shouldRecordTouch,
  touchMemberLastUsed,
} from "./MemberLastUsed";

const DOCS = "service:docs-viewer";
const BUILD = "terminal:terminal-4";

/** Stand in for the backend with one canned response for every call. */
function stubFetch(response: Partial<Response>): ReturnType<typeof vi.fn> {
  const mockFetch = vi.fn(() => Promise.resolve(response as Response));
  vi.stubGlobal("fetch", mockFetch);
  return mockFetch;
}

/** Stand in for a server that cannot be reached at all. */
function stubUnreachableFetch(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("offline"))),
  );
}

/** Put the module-level cache -- and the throttle's memory, which resets with
 *  it -- back to "nothing has been used". Both are machine-wide state, so each
 *  test has to start from the same place. */
async function resetCache(): Promise<void> {
  stubFetch({ ok: true, json: () => Promise.resolve({ last_used: {} }) });
  await loadMemberLastUsed();
  vi.unstubAllGlobals();
}

beforeEach(async () => {
  await resetCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("shouldRecordTouch", () => {
  it("records a ref this client has never recorded", () => {
    expect(shouldRecordTouch(DOCS, 1_000, {})).toBe(true);
  });

  it("suppresses a ref recorded within the throttle window", () => {
    expect(shouldRecordTouch(DOCS, 1_000 + TOUCH_THROTTLE_MS - 1, { [DOCS]: 1_000 })).toBe(false);
  });

  it("records a ref again once its window has expired", () => {
    expect(shouldRecordTouch(DOCS, 1_000 + TOUCH_THROTTLE_MS, { [DOCS]: 1_000 })).toBe(true);
  });

  it("keeps focus flapping between two panes silent", () => {
    // The decision is per ref, not "same as the last one recorded": clicking
    // back and forth between two panes alternates the ref on every click, and
    // a last-one-only memory would record every single flap. Here both panes
    // are already recorded, so a flap in either direction records nothing.
    const recorded = { [DOCS]: 1_000, [BUILD]: 2_000 };
    expect(shouldRecordTouch(DOCS, 3_000, recorded)).toBe(false);
    expect(shouldRecordTouch(BUILD, 4_000, recorded)).toBe(false);
  });
});

describe("the cached map", () => {
  it("loads the machine's recencies and answers from them", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ last_used: { [DOCS]: 1_700_000_000_000 } }) });
    await loadMemberLastUsed();

    expect(getMemberLastUsed()).toEqual({ [DOCS]: 1_700_000_000_000 });
  });

  it("treats an unreachable server as a machine where nothing was used", async () => {
    stubUnreachableFetch();
    expect(await fetchMemberLastUsed()).toEqual({});
  });

  it("takes a broadcast touch, and drops the entry a destroy carries", () => {
    applyMemberLastUsedChange(BUILD, 1_700_000_000_000);
    expect(getMemberLastUsed()).toEqual({ [BUILD]: 1_700_000_000_000 });

    // Null is "the object was destroyed", and the entry has to leave the map:
    // a reused ref must not rank on the strength of a dead one.
    applyMemberLastUsedChange(BUILD, null);
    expect(getMemberLastUsed()).toEqual({});
  });
});

describe("touchMemberLastUsed", () => {
  it("posts only the ref and caches the moment the server stamped", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: DOCS, at_ms: 1_700_000_000_000 }) });

    touchMemberLastUsed(DOCS, 1_000);
    await vi.waitFor(() => {
      expect(getMemberLastUsed()).toEqual({ [DOCS]: 1_700_000_000_000 });
    });

    // The body carries no timestamp: the server's clock is the authority,
    // which is what kills the clock-skew question.
    expect(mockFetch).toHaveBeenCalledWith("/api/member-last-used", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: DOCS }),
    });
  });

  it("does not write once per click when focus flaps between two panes", () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: DOCS, at_ms: 1 }) });

    // Six clicks alternating between two panes within the throttle window:
    // each pane is recorded once and the four flaps after that are silent.
    touchMemberLastUsed(DOCS, 1_000);
    touchMemberLastUsed(BUILD, 2_000);
    touchMemberLastUsed(DOCS, 3_000);
    touchMemberLastUsed(BUILD, 4_000);
    touchMemberLastUsed(DOCS, 5_000);
    touchMemberLastUsed(BUILD, 6_000);

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("records the same ref again once its window has expired", () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: DOCS, at_ms: 1 }) });

    touchMemberLastUsed(DOCS, 1_000);
    touchMemberLastUsed(DOCS, 1_000 + TOUCH_THROTTLE_MS - 1);
    touchMemberLastUsed(DOCS, 1_000 + TOUCH_THROTTLE_MS);

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("swallows an unreachable server, because a touch is fire-and-forget", () => {
    stubUnreachableFetch();

    expect(() => {
      touchMemberLastUsed(DOCS, 1_000);
    }).not.toThrow();
    expect(getMemberLastUsed()).toEqual({});
  });

  it("lets a destroyed ref be recorded again without waiting out the throttle", () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: BUILD, at_ms: 1 }) });

    touchMemberLastUsed(BUILD, 1_000);
    // The object is destroyed and its ref handed out again moments later: the
    // new object's first use must count, not sit behind the dead one's window.
    applyMemberLastUsedChange(BUILD, null);
    touchMemberLastUsed(BUILD, 2_000);

    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});
