import { describe, expect, it, vi } from "vitest";
import type { Participant } from "./Participants";

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

function participant(key: string, overrides: Partial<Participant> = {}): Participant {
  return {
    key,
    kind: "person",
    display_name: key,
    profile_picture_url: null,
    email: null,
    handle: key,
    color: "#000",
    is_owner: false,
    is_connected: true,
    last_seen: null,
    last_input_at: null,
    ...overrides,
  };
}

const ROSTER = [
  participant("user:owner-1", { display_name: "Josh", is_owner: true }),
  participant("user:bob-2", { display_name: "Bob", profile_picture_url: "https://pictures.example/bob.png" }),
  participant("workspace-agent", {
    display_name: "Atlas",
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

      answerRequests("user:bob-2", ROSTER);
      await vi.advanceTimersByTimeAsync(participants.RETRY_DELAY_MS - 1);
      expect(mockRequest).toHaveBeenCalledTimes(2);
      await vi.advanceTimersByTimeAsync(1);
      expect(mockRequest).toHaveBeenCalledTimes(4);
      expect(participants.getOwnParticipant()).toBe("user:bob-2");
      warn.mockRestore();
    } finally {
      vi.useRealTimers();
    }
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
