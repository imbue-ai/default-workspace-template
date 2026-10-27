// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: (id: string) => chats.get(id),
}));

import type { ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import type { TranscriptEvent, UserMessageEvent } from "../models/Response";
import { LastMessageMark, lastMessageAtMs } from "./LastMessageMark";

const CHAT_ID = "agent-1";
const NOW = Date.parse("2026-10-27T12:00:00Z");
const chats = new Map<string, ChatSnapshot>();

function event(type: string, timestamp: string): TranscriptEvent {
  return { type, event_id: `${type}-${timestamp}`, timestamp, source: "test" } as unknown as TranscriptEvent;
}

function userMessage(timestamp: string, display: Partial<UserMessageEvent> = {}): UserMessageEvent {
  return {
    type: "user_message",
    event_id: `u-${timestamp}`,
    timestamp,
    source: "test",
    role: "user",
    content: "hi",
    ...display,
  };
}

const reply = (timestamp: string): TranscriptEvent => event("assistant_message", timestamp);

function setChat(overrides: Partial<Omit<ChatSnapshot, "active_agent">> = {}): void {
  chats.set(CHAT_ID, chatSnapshotFixture(CHAT_ID, overrides));
}

describe("the last-message line under the latest message", () => {
  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    setChat();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    clearOutgoing(
      CHAT_ID,
      getOutgoingMessages(CHAT_ID).map((outgoing) => outgoing.id),
    );
    chats.clear();
  });

  it("dates the latest message from either side, by the transcript's own time", () => {
    const events = [userMessage("2026-10-24T09:00:00Z"), reply("2026-10-24T10:00:00Z")];
    expect(lastMessageAtMs(CHAT_ID, events)).toBe(Date.parse("2026-10-24T10:00:00Z"));
  });

  it("does not count a user message that is no turn of the conversation, such as an idle compaction", () => {
    const chip = userMessage("2026-10-27T11:58:00Z", { display: "chip", display_label: "Background task" });
    const compaction = userMessage("2026-10-27T11:59:00Z", { display: "status", compaction_cause: "idle" });
    const events = [reply("2026-10-24T10:00:00Z"), chip, compaction];
    expect(lastMessageAtMs(CHAT_ID, events)).toBe(Date.parse("2026-10-24T10:00:00Z"));
  });

  it("shows in every state but a working turn, a switch and this page's own send on its way", () => {
    const events = [reply("2026-10-24T10:00:00Z")];
    for (const status of ["idle", "attention", "background", "stopped"]) {
      setChat({ status });
      expect(lastMessageAtMs(CHAT_ID, events), status).not.toBeNull();
    }
    setChat({ status: "working" });
    expect(lastMessageAtMs(CHAT_ID, events)).toBeNull();
    setChat({ handoff: handoffStateFixture() });
    expect(lastMessageAtMs(CHAT_ID, events)).toBeNull();
    setChat();
    addOutgoing(CHAT_ID, "again");
    expect(lastMessageAtMs(CHAT_ID, events)).toBeNull();
  });

  it("shows nothing for a transcript with no message", () => {
    expect(lastMessageAtMs(CHAT_ID, [])).toBeNull();
  });

  it("reads as a sentence, with the exact time as its tooltip", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    const events = [userMessage("2026-10-24T09:00:00Z"), reply("2026-10-24T10:00:00Z")];
    m.mount(root, { view: () => m(LastMessageMark, { chatId: CHAT_ID, events }) });
    try {
      expect(root.querySelector(".last-message-mark")?.textContent).toBe("Last message 3 days ago");
      expect(root.querySelector(".last-message-mark__age")?.getAttribute("title")).toMatch(/^Last message .*2026/);
    } finally {
      m.mount(root, null);
      root.remove();
    }
  });
});
