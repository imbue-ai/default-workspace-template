import { describe, expect, it, vi } from "vitest";
import type { Participant } from "./Participants";
import { personFixture } from "./participantFixture";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
// The page is served under a base path, which every API URL and every origin-relative picture must carry.
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => `/base${path}` }));

// The page's own key and the roster are module-level state, so each test gets a fresh module.
async function loadParticipants(): Promise<typeof import("./Participants")> {
  vi.resetModules();
  mockRequest.mockReset();
  return import("./Participants");
}

const ROSTER = [
  personFixture("user:owner-1", "Josh", { is_owner: true }),
  personFixture("user:bob-2", "Bob", { profile_picture_url: "https://pictures.example/bob.png" }),
  personFixture("workspace-agent", "Atlas", {
    kind: "workspace_agent",
    handle: "agent",
    profile_picture_url: "/api/participants/workspace-agent/picture",
  }),
];

function answerRequests(me: string, roster: Participant[]): void {
  mockRequest.mockImplementation(({ url }: { url: string }) => {
    if (url === "/base/api/me") return Promise.resolve({ participant: me });
    if (url === "/base/api/participants") return Promise.resolve({ participants: roster });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

describe("ensureParticipants", () => {
  it("loads the page's own key and the roster once under the base path, and later calls share the load", async () => {
    const participants = await loadParticipants();
    answerRequests("user:bob-2", ROSTER);

    const first = participants.ensureParticipants();
    const second = participants.ensureParticipants();
    await Promise.all([first, second]);
    await participants.ensureParticipants();

    expect(mockRequest).toHaveBeenCalledTimes(2);
    expect(participants.getOwnParticipant()).toBe("user:bob-2");
    expect(participants.getParticipant("user:bob-2")?.display_name).toBe("Bob");
    expect(participants.getOwner()?.key).toBe("user:owner-1");
    expect(participants.getWorkspaceAgent()?.display_name).toBe("Atlas");
    // The agent's picture is this app's, so it is reached under the base path; Bob's is wherever it is.
    expect(participants.getWorkspaceAgent()?.profile_picture_url).toBe(
      "/base/api/participants/workspace-agent/picture",
    );
    expect(participants.getParticipant("user:bob-2")?.profile_picture_url).toBe("https://pictures.example/bob.png");
  });

  it("leaves the page as an owner's page would be after a failed load, and asks again by itself later", async () => {
    vi.useFakeTimers();
    try {
      const participants = await loadParticipants();
      const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      mockRequest.mockRejectedValue(new Error("offline"));

      await participants.ensureParticipants();

      expect(mockRequest).toHaveBeenCalledTimes(2);
      expect(participants.getOwnParticipant()).toBeNull();
      expect(participants.isViewerOwner()).toBe(false);
      expect(warn).toHaveBeenCalledTimes(1);

      // Asked again while the app is still unreachable (a focus, a mentions push): the load runs, since nothing
      // is cached, but the retry already pending is the one retry there is.
      await participants.ensureParticipants();
      expect(mockRequest).toHaveBeenCalledTimes(4);

      answerRequests("user:bob-2", ROSTER);
      await vi.advanceTimersByTimeAsync(participants.RETRY_DELAY_MS - 1);
      expect(mockRequest).toHaveBeenCalledTimes(4);
      await vi.advanceTimersByTimeAsync(1);
      expect(mockRequest).toHaveBeenCalledTimes(6);
      expect(participants.getOwnParticipant()).toBe("user:bob-2");
      await vi.advanceTimersByTimeAsync(participants.RETRY_DELAY_MS);
      expect(mockRequest).toHaveBeenCalledTimes(6);
      warn.mockRestore();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ensureParticipants again", () => {
  it("reads the page's own key and the roster again once the last read is older than the cache", async () => {
    vi.useFakeTimers();
    try {
      const participants = await loadParticipants();
      answerRequests("user:bob-2", ROSTER);
      await participants.ensureParticipants();
      expect(mockRequest).toHaveBeenCalledTimes(2);

      // Within the cache the answer stands; a burst of asks (a focus, a visibility change, a mentions push) is one read.
      await vi.advanceTimersByTimeAsync(participants.PARTICIPANTS_FRESH_MS - 1);
      await participants.ensureParticipants();
      expect(mockRequest).toHaveBeenCalledTimes(2);

      await vi.advanceTimersByTimeAsync(1);
      answerRequests("user:bob-2", [...ROSTER, personFixture("user:carol-3", "Carol")]);
      await participants.ensureParticipants();
      expect(mockRequest).toHaveBeenCalledTimes(4);
      expect(participants.getParticipant("user:carol-3")?.display_name).toBe("Carol");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("ensureParticipantsKnowAuthors", () => {
  it("reads the roster again for a turn whose author the page does not know or calls by another name", async () => {
    vi.useFakeTimers();
    try {
      const participants = await loadParticipants();
      answerRequests("user:owner-1", ROSTER);
      await participants.ensureParticipants();
      await vi.advanceTimersByTimeAsync(participants.PARTICIPANTS_FRESH_MS);
      expect(mockRequest).toHaveBeenCalledTimes(2);

      // Every author already named as the turns name them, and the owner under either key: nothing to read.
      participants.ensureParticipantsKnowAuthors([
        { author: "user:bob-2", author_name: "Bob" },
        { author: "owner", author_name: "Owner" },
        { author: "user:owner-1", author_name: "Joshua" },
        {},
      ]);
      await vi.advanceTimersByTimeAsync(0);
      expect(mockRequest).toHaveBeenCalledTimes(2);

      answerRequests("user:owner-1", [...ROSTER, personFixture("user:carol-3", "Carol")]);
      participants.ensureParticipantsKnowAuthors([{ author: "user:carol-3", author_name: "Carol" }]);
      await vi.advanceTimersByTimeAsync(0);
      expect(mockRequest).toHaveBeenCalledTimes(4);
      expect(participants.getParticipant("user:carol-3")?.display_name).toBe("Carol");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("isOwnerKey", () => {
  it("knows the owner under the unshared key and under the account the roster marks, and no one else", async () => {
    const participants = await loadParticipants();
    participants.setParticipantsForTesting("user:bob-2", ROSTER);

    expect(participants.isOwnerKey("owner")).toBe(true);
    expect(participants.isOwnerKey("user:owner-1")).toBe(true);
    expect(participants.isOwnerKey("user:bob-2")).toBe(false);
    expect(participants.isOwnerKey("user:nobody-9")).toBe(false);
  });
});

describe("isViewerOwner", () => {
  it("knows the owner under the unshared key and under their account key, and no one else", async () => {
    const participants = await loadParticipants();

    participants.setParticipantsForTesting("owner", []);
    expect(participants.isViewerOwner()).toBe(true);
    participants.setParticipantsForTesting("user:owner-1", ROSTER);
    expect(participants.isViewerOwner()).toBe(true);
    participants.setParticipantsForTesting("user:bob-2", ROSTER);
    expect(participants.isViewerOwner()).toBe(false);
  });
});

describe("participantName", () => {
  it("prefers the roster's name, then the name the turn carried, then the key", async () => {
    const participants = await loadParticipants();
    participants.setParticipantsForTesting("owner", ROSTER);

    expect(participants.participantName("user:bob-2", "Robert")).toBe("Bob");
    expect(participants.participantName("name:Someone", "Someone")).toBe("Someone");
    expect(participants.participantName("email:x@example.com", null)).toBe("email:x@example.com");
  });
});
