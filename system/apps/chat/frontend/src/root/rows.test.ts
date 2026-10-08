import { describe, expect, it } from "vitest";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import type { ProvisionalChat } from "../models/Chats";
import { defaultGroupSize, groupedRows, parentOf, rowsFromSnapshots } from "./rows";

const NONE: ReadonlySet<string> = new Set();

function provisional(chatId: string, phase: ProvisionalChat["phase"], name = ""): ProvisionalChat {
  return { chat_id: chatId, name, account_id: "", phase, error: null, is_seeded: false, is_default: false };
}

describe("rowsFromSnapshots", () => {
  it("lists every chat, then the provisional chats the list does not name yet", () => {
    const rows = rowsFromSnapshots(
      [chatSnapshotFixture("agent-a", { title: "Plan", last_messaged_at: 1_700_000_000 })],
      [provisional("agent-a", "creating"), provisional("agent-b", "failed")],
    );

    expect(rows.map((row) => [row.chatId, row.title, row.status, row.lastActiveMs, row.isProvisional])).toEqual([
      ["agent-a", "Plan", "idle", 1_700_000_000_000, false],
      ["agent-b", "New chat", "error", null, true],
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
    );

    expect(parentOf(rows[0], rows)).toBeNull();
    expect(groupedRows(rows, NONE).map((row) => row.chatId)).toEqual(["agent-orphan"]);
  });

  it("leads with the default chat and its helpers whatever was messaged since, a chat started here included", () => {
    const rows = rowsFromSnapshots(
      [
        chatSnapshotFixture("agent-recent", { last_messaged_at: 300 }),
        chatSnapshotFixture("agent-welcome", { last_messaged_at: 100, is_default: true }),
        chatSnapshotFixture("agent-helper", {
          last_messaged_at: 50,
          labels: { agent_created: "true", lead_agent: "agent-welcome" },
        }),
        chatSnapshotFixture("agent-mine"),
      ],
      [],
    );

    const grouped = groupedRows(rows, new Set(["agent-mine"]));

    expect(grouped.map((row) => row.chatId)).toEqual(["agent-welcome", "agent-helper", "agent-mine", "agent-recent"]);
    expect(defaultGroupSize(grouped)).toBe(2);
  });

  it("leads with a default chat still awaiting its first send", () => {
    const welcome: ProvisionalChat = { ...provisional("agent-welcome", "awaiting_first_send"), is_default: true };
    const rows = rowsFromSnapshots([chatSnapshotFixture("agent-recent", { last_messaged_at: 300 })], [welcome]);

    const grouped = groupedRows(rows, NONE);

    expect(grouped.map((row) => [row.chatId, row.isDefault])).toEqual([
      ["agent-welcome", true],
      ["agent-recent", false],
    ]);
    expect(defaultGroupSize(grouped)).toBe(1);
  });

  it("has no default group when no chat is the default", () => {
    const rows = rowsFromSnapshots([chatSnapshotFixture("agent-a"), chatSnapshotFixture("agent-b")], []);

    expect(defaultGroupSize(groupedRows(rows, NONE))).toBe(0);
  });
});
