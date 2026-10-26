import { describe, expect, it, vi } from "vitest";

// Capture mithril's request so a test drives the backend's answers without a network call; apiUrl is
// identity so URLs are predictable.
const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import type { IdentityMatch } from "./IdentityMatches";

function match(overrides: Partial<IdentityMatch> = {}): IdentityMatch {
  return {
    match_id: "identity-1",
    chat_id: "agent-1",
    searched_for: "Ada Lovelace, London",
    candidates: [
      {
        name: "Ada Lovelace",
        summary: "Mathematician",
        source_url: "https://www.example.org/ada",
        source_title: "Ada",
      },
    ],
    status: "pending",
    chosen_index: null,
    ...overrides,
  };
}

async function freshModule(): Promise<typeof import("./IdentityMatches")> {
  vi.resetModules();
  mockRequest.mockReset();
  return import("./IdentityMatches");
}

describe("the identity-match store", () => {
  it("shows a pending match for its own chat only", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());

    expect(matches.getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-1");
    expect(matches.getPendingIdentityMatch("agent-2")).toBeNull();
  });

  it("closes the popup when the match is answered or superseded, but not for another match", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());

    matches.applyIdentityMatchUpdate(match({ match_id: "identity-old", status: "superseded" }));
    expect(matches.getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-1");

    matches.applyIdentityMatchUpdate(match({ status: "rejected" }));
    expect(matches.getPendingIdentityMatch("agent-1")).toBeNull();
  });

  it("a newer pending match replaces the shown one", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());
    matches.applyIdentityMatchUpdate(match({ match_id: "identity-2" }));

    expect(matches.getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-2");
  });

  it("a match set aside stays hidden on this page; a reconnect's replay starts from nothing", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());
    matches.setIdentityMatchAside("identity-1");
    expect(matches.getPendingIdentityMatch("agent-1")).toBeNull();

    matches.forgetIdentityMatches();
    matches.applyIdentityMatchUpdate(match({ match_id: "identity-2" }));
    expect(matches.getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-2");
  });

  it("confirms by candidate index and takes in the answered match", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());
    mockRequest.mockResolvedValueOnce(match({ status: "confirmed", chosen_index: 0 }));

    await matches.confirmIdentityMatch("identity-1", 0);

    expect(mockRequest).toHaveBeenLastCalledWith({
      method: "POST",
      url: "/api/identity-matches/:matchId/confirm",
      params: { matchId: "identity-1" },
      body: { candidate_index: 0 },
    });
    expect(matches.getPendingIdentityMatch("agent-1")).toBeNull();
  });

  it("rejects with no body", async () => {
    const matches = await freshModule();
    mockRequest.mockResolvedValueOnce(match({ status: "rejected" }));

    await matches.rejectIdentityMatch("identity-1");

    expect(mockRequest).toHaveBeenLastCalledWith(
      expect.objectContaining({ method: "POST", url: "/api/identity-matches/:matchId/reject", body: undefined }),
    );
  });

  it("a failed answer leaves the match pending", async () => {
    const matches = await freshModule();
    matches.applyIdentityMatchUpdate(match());
    mockRequest.mockRejectedValueOnce(new Error("offline"));

    await expect(matches.rejectIdentityMatch("identity-1")).rejects.toThrow("offline");
    expect(matches.getPendingIdentityMatch("agent-1")?.match_id).toBe("identity-1");
  });
});

describe("sourceSite", () => {
  it("names the site without www, and falls back to the text for a value that is not a URL", async () => {
    const matches = await freshModule();
    expect(matches.sourceSite("https://www.example.org/ada?x=1")).toBe("example.org");
    expect(matches.sourceSite("https://news.example.com/a")).toBe("news.example.com");
    expect(matches.sourceSite("not a url")).toBe("not a url");
  });
});
