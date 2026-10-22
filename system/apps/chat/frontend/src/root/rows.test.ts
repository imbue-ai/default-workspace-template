import { describe, expect, it } from "vitest";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import type { ProvisionalChat } from "../models/Chats";
import { groupedRows, parentOf, rowsFromSnapshots, startsWithMainChat } from "./rows";

const NONE: ReadonlySet<string> = new Set();

function provisional(chatId: string, phase: ProvisionalChat["phase"], name = ""): ProvisionalChat {
  return { chat_id: chatId, name, account_id: "", phase, error: null, is_seeded: false };
}

describe("rowsFromSnapshots", () => {
  it("lists every chat, then the provisional chats the list does not name yet", () => {
    const rows = rowsFromSnapshots(
      [chatSnapshotFixture("agent-a", { title: "Plan", last_messaged_at: 1_700_000_000 })],
      [provisional("agent-a", "creating"), provisional("agent-b", "failed")],
      null,
    );

    expect(rows.map((row) => [row.chatId, row.title, row.status, row.lastActiveMs, row.kind])).toEqual([
      ["agent-a", "Plan", "idle", 1_700_000_000_000, "agent"],
      ["agent-b", "New chat", "error", null, "provisional"],
    ]);
  });
});

describe("groupedRows", () => {
  it("orders by recency with never-messaged chats last, and a chat started here first", () => {
    const rows = rowsFromSnapshots(
      [
        chatSnapshotFixture("agent-old", { last_messaged_at: 100 }),
        chatSnapshotFixture("agent-never"),
        chatSnapshotFixture("agent-new", { last_messaged_at: 200 }),
        chatSnapshotFixture("agent-mine"),
      ],
      [],
      null,
    );

    expect(groupedRows(rows, new Set(["agent-mine"])).map((row) => row.chatId)).toEqual([
      "agent-mine",
      "agent-new",
      "agent-old",
      "agent-never",
    ]);
  });

  it("files a worker under the chat that owns its lead agent, and moves the pair together", () => {
    const rows = rowsFromSnapshots(
      [
        chatSnapshotFixture("agent-lead", { last_messaged_at: 100, agent_ids: ["agent-first", "agent-lead"] }),
        chatSnapshotFixture("agent-other", { last_messaged_at: 150 }),
        chatSnapshotFixture("agent-worker", {
          last_messaged_at: 300,
          labels: { agent_created: "true", lead_agent: "agent-first" },
        }),
        chatSnapshotFixture("agent-grandchild", {
          last_messaged_at: 50,
          labels: { agent_created: "true", lead_agent: "agent-worker" },
        }),
      ],
      [],
      null,
    );

    const grouped = groupedRows(rows, NONE);

    expect(grouped.map((row) => row.chatId)).toEqual([
      "agent-lead",
      "agent-worker",
      "agent-grandchild",
      "agent-other",
    ]);
    expect(parentOf(grouped[2], rows)?.chatId).toBe("agent-lead");
  });

  it("keeps a worker whose lead is not listed as a root of its own", () => {
    const rows = rowsFromSnapshots(
      [chatSnapshotFixture("agent-orphan", { labels: { agent_created: "true", lead_agent: "agent-gone" } })],
      [],
      null,
    );

    expect(parentOf(rows[0], rows)).toBeNull();
    expect(groupedRows(rows, NONE).map((row) => row.chatId)).toEqual(["agent-orphan"]);
  });

  it("leads with the main chat and its helpers whatever was messaged since, a chat started here included", () => {
    const rows = rowsFromSnapshots(
      [
        chatSnapshotFixture("agent-recent", { last_messaged_at: 300 }),
        chatSnapshotFixture("agent-welcome", { last_messaged_at: 100 }),
        chatSnapshotFixture("agent-helper", {
          last_messaged_at: 50,
          labels: { agent_created: "true", lead_agent: "agent-welcome" },
        }),
        chatSnapshotFixture("agent-mine"),
      ],
      [],
      "agent-welcome",
    );

    const grouped = groupedRows(rows, new Set(["agent-mine"]));

    expect(grouped.map((row) => row.chatId)).toEqual(["agent-welcome", "agent-helper", "agent-mine", "agent-recent"]);
    expect(startsWithMainChat(grouped)).toBe(true);
  });

  it("leads with a main chat still awaiting its first send", () => {
    const rows = rowsFromSnapshots(
      [chatSnapshotFixture("agent-recent", { last_messaged_at: 300 })],
      [provisional("agent-welcome", "awaiting_first_send")],
      "agent-welcome",
    );

    const grouped = groupedRows(rows, NONE);

    expect(grouped.map((row) => [row.chatId, row.isMain])).toEqual([
      ["agent-welcome", true],
      ["agent-recent", false],
    ]);
    expect(startsWithMainChat(grouped)).toBe(true);
  });

  it("has no main group when no chat is the main chat", () => {
    const rows = rowsFromSnapshots([chatSnapshotFixture("agent-a"), chatSnapshotFixture("agent-b")], [], null);

    expect(startsWithMainChat(groupedRows(rows, NONE))).toBe(false);
  });
});

describe("rowsFromSnapshots with rooms", () => {
  const room = (id: string, title: string, awaiting: string[] = [], lastMessagedAt: number | null = null) => ({
    id,
    kind: "room" as const,
    title,
    last_messaged_at: lastMessagedAt,
    awaiting_chat_ids: awaiting,
  });

  it("lists rooms as a third source, with the mention mark the viewer's unseen mentions give", () => {
    const rows = rowsFromSnapshots(
      [chatSnapshotFixture("agent-a", { title: "Plan" })],
      [provisional("agent-b", "awaiting_first_send")],
      null,
      [room("room-0123456789abcdef", "Lounge", [], 1_700_000_000)],
      new Set(["room-0123456789abcdef"]),
    );

    expect(rows.map((row) => [row.chatId, row.kind, row.title, row.lastActiveMs, row.hasMention])).toEqual([
      ["agent-a", "agent", "Plan", null, false],
      ["room-0123456789abcdef", "room", "Lounge", 1_700_000_000_000, true],
      ["agent-b", "provisional", "New chat", null, false],
    ]);
  });

  it("works while an agent chat the room asked is working, and is idle otherwise", () => {
    const working = chatSnapshotFixture("agent-a", { status: "working" });
    const idle = chatSnapshotFixture("agent-b", { status: "idle" });
    const asked = room("room-0123456789abcdef", "Lounge", ["agent-a"]);
    const unasked = room("room-fedcba9876543210", "Hall", ["agent-b"]);

    const statuses = rowsFromSnapshots([working, idle], [], null, [asked, unasked]).map((row) => row.status);

    expect(statuses).toEqual(["working", "idle", "working", "idle"]);
  });

  it("lists an awaiting chat turned into a room under its id once, as the room", () => {
    const rows = rowsFromSnapshots([], [provisional("agent-x", "awaiting_first_send")], null, [
      room("agent-x", "Chat 2"),
    ]);

    expect(rows.map((row) => [row.chatId, row.kind])).toEqual([["agent-x", "room"]]);
  });
});
