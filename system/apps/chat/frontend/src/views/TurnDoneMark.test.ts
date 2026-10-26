// @vitest-environment jsdom
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: (id: string) => chats.get(id),
}));

import type { ActiveAgent, ChatSnapshot } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { addOutgoing, clearOutgoing, getOutgoingMessages, noteBackendArrivals } from "../models/OutgoingMessages";
import { appendEvents, type TranscriptEvent } from "../models/Response";
import { TurnDoneMark, turnDoneLabel } from "./TurnDoneMark";

const CHAT_ID = "agent-1";
const chats = new Map<string, ChatSnapshot>();

function userMessage(timestamp: string): TranscriptEvent {
  return { type: "user_message", event_id: `u-${timestamp}`, timestamp, source: "test", role: "user", content: "hi" };
}

function reply(timestamp: string, isApiError = false): TranscriptEvent {
  return {
    type: "assistant_message",
    event_id: `a-${timestamp}`,
    timestamp,
    source: "test",
    model: "test-model",
    text: "done",
    tool_calls: [],
    stop_reason: "end_turn",
    usage: null,
    is_auth_error: false,
    is_api_error: isApiError,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

const finishedTurn = [userMessage("2026-10-20T10:00:00Z"), reply("2026-10-20T10:00:14Z")];

function setChat(
  overrides: Partial<Omit<ChatSnapshot, "active_agent">> & { active_agent?: Partial<ActiveAgent> } = {},
): void {
  chats.set(
    CHAT_ID,
    chatSnapshotFixture(CHAT_ID, {
      ...overrides,
      active_agent: { activity_state: "IDLE", ...overrides.active_agent },
    }),
  );
}

describe("the done mark under the latest reply", () => {
  beforeEach(() => {
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    setChat();
  });

  afterEach(() => {
    clearOutgoing(
      CHAT_ID,
      getOutgoingMessages(CHAT_ID).map((message) => message.id),
    );
    chats.clear();
    vi.restoreAllMocks();
  });

  /** Live the turn as this page would: the user sends at `sentAt`, the message lands, and the reply
   *  reaches the page at `replyAt` (epoch ms on the page's clock). */
  function liveTurn(sentAt: number, replyAt: number): TranscriptEvent[] {
    const clock = vi.spyOn(Date, "now").mockReturnValue(sentAt);
    const opening = userMessage(new Date(sentAt + 3_000).toISOString());
    const answer = reply(new Date(replyAt - 300).toISOString());
    addOutgoing(CHAT_ID, "hi");
    appendEvents(CHAT_ID, [opening]);
    clock.mockReturnValue(replyAt);
    appendEvents(CHAT_ID, [answer]);
    clock.mockRestore();
    return [opening, answer];
  }

  it("says the turn is done, and how long the user waited, from their send to the reply reaching the page", () => {
    const events = liveTurn(Date.UTC(2026, 9, 20, 10, 0, 0), Date.UTC(2026, 9, 20, 10, 0, 23, 400));
    expect(turnDoneLabel(CHAT_ID, events)).toBe("Done in 23s");

    const root = document.createElement("div");
    m.render(root, m(TurnDoneMark, { chatId: CHAT_ID, events }));
    expect(root.querySelector(".turn-done-mark .turn-done-mark__label")?.textContent).toBe("Done in 23s");
    expect(root.querySelector(".turn-done-mark__check svg")).not.toBeNull();
  });

  it("says only Done for a wait under a second", () => {
    const events = liveTurn(Date.UTC(2026, 9, 20, 11, 0, 0), Date.UTC(2026, 9, 20, 11, 0, 0, 800));
    expect(turnDoneLabel(CHAT_ID, events)).toBe("Done");
  });

  it("says only Done for a turn this page did not see sent", () => {
    expect(turnDoneLabel(CHAT_ID, finishedTurn)).toBe("Done");
  });

  it("says only Done when the page did not see the turn's last work arrive", () => {
    const sent = userMessage("2026-10-20T12:00:00Z");
    addOutgoing(CHAT_ID, "hi");
    noteBackendArrivals(CHAT_ID, [sent.event_id]);
    expect(turnDoneLabel(CHAT_ID, [sent, reply("2026-10-20T12:00:09Z")])).toBe("Done");
  });

  it("shows as soon as the agent's turn is over, while its harness still closes it", () => {
    setChat({ status: "working", active_agent: { activity_state: "IDLE" } });
    expect(turnDoneLabel(CHAT_ID, finishedTurn)).toBe("Done");
  });

  it.each([
    ["working", { status: "working", active_agent: { activity_state: "THINKING" } }],
    ["waiting on a background task", { status: "background" }],
    ["waiting on a permission answer", { status: "attention" }],
    ["stopped", { status: "stopped" }],
    ["switching to another harness", { handoff: handoffStateFixture() }],
    [
      "holding a queued message",
      { active_agent: { queued_messages: [{ queued_id: "q1", content: "next", timestamp: "" }] } },
    ],
    ["connecting", { active_agent: { is_connecting: true } }],
  ] as const)("shows nothing while the chat is %s", (_name, overrides) => {
    setChat(overrides as Parameters<typeof setChat>[0]);
    expect(turnDoneLabel(CHAT_ID, finishedTurn)).toBeNull();
  });

  it("gives way to a send of this page's on its way", () => {
    addOutgoing(CHAT_ID, "and one more thing");
    expect(turnDoneLabel(CHAT_ID, finishedTurn)).toBeNull();
  });

  it("leaves a turn that ended on a provider failure to that failure's notice", () => {
    expect(
      turnDoneLabel(CHAT_ID, [userMessage("2026-10-20T10:00:00Z"), reply("2026-10-20T10:00:03Z", true)]),
    ).toBeNull();
  });

  it("shows nothing for a turn the agent has not answered, or a chat with no transcript", () => {
    expect(turnDoneLabel(CHAT_ID, [userMessage("2026-10-20T10:00:00Z")])).toBeNull();
    expect(turnDoneLabel(CHAT_ID, [])).toBeNull();
  });
});
