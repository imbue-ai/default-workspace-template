import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// apiUrl reads the base path from a <meta> tag, which vitest's node environment
// has no document for; identity keeps the asserted URLs the bare /api paths.
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));

import {
  applyMemberTitleChange,
  displayNameForMember,
  displayNameForRef,
  fetchMemberTitles,
  getMemberTitle,
  getMemberTitles,
  loadMemberTitles,
  moveMemberTitle,
  nextFreeAutoName,
  setMemberTitle,
} from "./MemberTitles";

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

/** Put the module-level cache back to "nothing has been renamed". The cache is
 *  machine-wide state, so each test has to start from the same place. */
async function resetCache(): Promise<void> {
  stubFetch({ ok: true, json: () => Promise.resolve({ titles: {} }) });
  await loadMemberTitles();
  vi.unstubAllGlobals();
}

beforeEach(async () => {
  await resetCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("displayNameForRef", () => {
  it("falls back to the derived name when the object was never renamed", () => {
    expect(displayNameForRef({}, DOCS, "docs-viewer")).toBe("docs-viewer");
  });

  it("prefers the chosen name over the derived one", () => {
    expect(displayNameForRef({ [DOCS]: "Docs" }, DOCS, "docs-viewer")).toBe("Docs");
  });

  it("names the object, so the same ref reads the same whatever view asks", () => {
    // The point of keying by ref: two views resolving the same object hand in
    // whatever each derived for itself, and both come back with one name.
    const titles = { [DOCS]: "Docs" };
    expect(displayNameForRef(titles, DOCS, "docs-viewer")).toBe(displayNameForRef(titles, DOCS, "Docs viewer"));
  });

  it("reads a legacy panel title when the store has no name for the ref", () => {
    // A layout saved before names were filed by ref is still carrying one on
    // the panel; the object must not lose its name on upgrade.
    expect(displayNameForRef({}, DOCS, "docs-viewer", "Old docs")).toBe("Old docs");
  });

  it("lets the store win over a stale legacy panel title", () => {
    // The saved layout still says "Old docs" and always will -- nothing writes
    // that field any more -- so the rename that happened since has to win.
    expect(displayNameForRef({ [DOCS]: "Docs" }, DOCS, "docs-viewer", "Old docs")).toBe("Docs");
  });

  it("ignores an empty name on either side", () => {
    expect(displayNameForRef({ [DOCS]: "" }, DOCS, "docs-viewer")).toBe("docs-viewer");
    expect(displayNameForRef({}, DOCS, "docs-viewer", "")).toBe("docs-viewer");
  });
});

describe("the cached map", () => {
  it("loads the machine's names and answers from them", async () => {
    stubFetch({ ok: true, json: () => Promise.resolve({ titles: { [DOCS]: "Docs" } }) });
    await loadMemberTitles();

    expect(getMemberTitles()).toEqual({ [DOCS]: "Docs" });
    expect(getMemberTitle(DOCS)).toBe("Docs");
    expect(getMemberTitle(BUILD)).toBeNull();
    expect(displayNameForMember(DOCS, "docs-viewer")).toBe("Docs");
  });

  it("treats an unreachable server as a machine where nothing was renamed", async () => {
    stubUnreachableFetch();
    expect(await fetchMemberTitles()).toEqual({});
  });

  it("takes a broadcast rename, and drops the name a cleared one carries", () => {
    applyMemberTitleChange(BUILD, "Build");
    expect(displayNameForMember(BUILD, "terminal-4")).toBe("Build");

    // Null is both "the user cleared it" and "the object was destroyed", and
    // both have to leave the map -- a reused ref must not inherit a dead name.
    applyMemberTitleChange(BUILD, null);
    expect(getMemberTitle(BUILD)).toBeNull();
    expect(displayNameForMember(BUILD, "terminal-4")).toBe("terminal-4");
  });
});

describe("setMemberTitle", () => {
  it("posts the ref and caches the name the server kept", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ ref: DOCS, title: "Docs" }) });

    expect(await setMemberTitle(DOCS, "  Docs  ")).toBe("Docs");
    expect(mockFetch).toHaveBeenCalledWith("/api/member-titles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: DOCS, title: "  Docs  " }),
    });
    // The server trims, so what is cached is its answer rather than what was
    // typed.
    expect(getMemberTitle(DOCS)).toBe("Docs");
  });

  it("clears the name when the server reports the object is unnamed again", async () => {
    applyMemberTitleChange(DOCS, "Docs");
    stubFetch({ ok: true, json: () => Promise.resolve({ ref: DOCS, title: null }) });

    expect(await setMemberTitle(DOCS, "   ")).toBeNull();
    expect(getMemberTitle(DOCS)).toBeNull();
  });

  it("throws with the server's reason and leaves the cache alone", async () => {
    stubFetch({ ok: false, status: 400, json: () => Promise.resolve({ detail: "Title is 500 characters" }) });

    await expect(setMemberTitle(DOCS, "a".repeat(500))).rejects.toThrow("Title is 500 characters");
    expect(getMemberTitle(DOCS)).toBeNull();
  });
});

describe("nextFreeAutoName", () => {
  it("starts at 1 on an empty machine", () => {
    expect(nextFreeAutoName("Chat", new Set())).toBe("Chat 1");
  });

  it("fills the gap a destroyed object left rather than counting past it", () => {
    // Destroy clears the title server-side, so "Chat 2" is free again and the
    // next create takes it -- the desired "first free" behavior.
    expect(nextFreeAutoName("Chat", new Set(["Chat 1", "Chat 3"]))).toBe("Chat 2");
  });

  it("counts past a full run of taken slots", () => {
    expect(nextFreeAutoName("Terminal", new Set(["Terminal 1", "Terminal 2"]))).toBe("Terminal 3");
  });

  it("treats a user-typed name as taken whatever its casing", () => {
    // A user who renamed something to "chat 1" by hand still blocks the slot;
    // trailing whitespace does not sneak a duplicate past either.
    expect(nextFreeAutoName("Chat", new Set(["chat 1", " CHAT 2 "]))).toBe("Chat 3");
  });

  it("collides with derived labels too, not just chosen names", () => {
    // The caller feeds in every derived label as well, so an agent whose
    // petname-derived label happens to read "Chat 2" blocks that slot.
    expect(nextFreeAutoName("Chat", new Set(["green-triumphant-trout", "Chat 2", "Chat 1"]))).toBe("Chat 3");
  });

  it("ignores names of other kinds and near-misses", () => {
    expect(nextFreeAutoName("Chat", new Set(["Terminal 1", "Chat", "Chat 1a", "MyChat 1"]))).toBe("Chat 1");
  });
});

describe("moveMemberTitle", () => {
  it("carries a chosen name onto the ref the object answers to now", async () => {
    applyMemberTitleChange(BUILD, "Build");
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ title: "Build" }) });

    await moveMemberTitle(BUILD, "terminal:release");

    // Renaming the tmux session renames the object's ref, not the object: the
    // name the user chose follows it, and the old entry goes.
    expect(mockFetch).toHaveBeenCalledTimes(2);
    expect(mockFetch).toHaveBeenNthCalledWith(1, "/api/member-titles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "terminal:release", title: "Build" }),
    });
    expect(mockFetch).toHaveBeenNthCalledWith(2, "/api/member-titles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: BUILD, title: "" }),
    });
  });

  it("writes nothing for an object nobody renamed", async () => {
    const mockFetch = stubFetch({ ok: true, json: () => Promise.resolve({ title: null }) });

    await moveMemberTitle(BUILD, "terminal:release");

    expect(mockFetch).not.toHaveBeenCalled();
  });
});
