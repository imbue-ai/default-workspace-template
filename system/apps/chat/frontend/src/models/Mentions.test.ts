import { describe, expect, it, vi } from "vitest";
import type { MentionHandle } from "./Mentions";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./Chats", () => ({ addMentionsUpdatedListener: vi.fn() }));

const TABLE: MentionHandle[] = [
  { handle: "Josh", display_name: "Josh", kind: "person", key: "user:josh", chat_id: null },
  { handle: "Bob", display_name: "Robert", kind: "person", key: "user:bob", chat_id: null },
  { handle: "agent", display_name: "Atlas", kind: "workspace_agent", key: "workspace-agent", chat_id: null },
  { handle: "Main-chat", display_name: "Main chat", kind: "chat", key: null, chat_id: "agent-1" },
];

async function loadMentions(): Promise<typeof import("./Mentions")> {
  vi.resetModules();
  mockRequest.mockReset();
  return import("./Mentions");
}

describe("matchingHandles", () => {
  it("puts the handles starting with the query first, then those containing it, in the table's order", async () => {
    const { matchingHandles } = await loadMentions();

    expect(matchingHandles("", TABLE).map((entry) => entry.handle)).toEqual(["Josh", "Bob", "agent", "Main-chat"]);
    expect(matchingHandles("a", TABLE).map((entry) => entry.handle)).toEqual(["agent", "Main-chat"]);
    expect(matchingHandles("rob", TABLE).map((entry) => entry.handle)).toEqual(["Bob"]);
    expect(matchingHandles("zzz", TABLE)).toEqual([]);
  });
});

describe("ensureMentionHandles", () => {
  it("reads the table once while it is fresh, and keeps the last one past a failed read", async () => {
    const mentions = await loadMentions();
    mockRequest.mockResolvedValueOnce({ handles: TABLE });

    await mentions.ensureMentionHandles();
    await mentions.ensureMentionHandles();

    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(mentions.getMentionHandles()).toEqual(TABLE);
  });
});

describe("watchPendingMentions", () => {
  it("reads the viewer's unseen mentions into a set of chat ids", async () => {
    const mentions = await loadMentions();
    mockRequest.mockResolvedValueOnce({
      mentions: [
        { chat_id: "room-1", message_id: "m-1", sender: "user:josh" },
        { chat_id: "room-1", message_id: "m-2", sender: "user:josh" },
      ],
    });

    await mentions.watchPendingMentions();

    expect([...mentions.getMentionedChatIds()]).toEqual(["room-1"]);
  });
});
