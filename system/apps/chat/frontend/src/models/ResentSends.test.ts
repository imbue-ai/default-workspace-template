import { describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { request: vi.fn(), redraw: vi.fn() } }));

const mocks = vi.hoisted(() => ({
  resentSendsByChat: new Map<string, { text: string; sent_after: number; closed_at: number | null }[]>(),
}));

vi.mock("./Chats", () => ({
  getChatById: (chatId: string) => {
    const resentSends = mocks.resentSendsByChat.get(chatId);
    return resentSends === undefined ? undefined : { resent_sends: resentSends };
  },
}));

import type { TranscriptEvent } from "./Response";
import { getResentSendsKey, resentTurnsIn } from "./ResentSends";

const SECOND = 1_800_000_000;

function userTurn(id: string, content: string, atSecond: number): TranscriptEvent {
  return {
    timestamp: new Date(atSecond * 1000).toISOString(),
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content,
  };
}

function resendHello(chatId: string, closedAt: number | null = null): void {
  mocks.resentSendsByChat.set(chatId, [{ text: "hello", sent_after: SECOND, closed_at: closedAt }]);
}

describe("resentTurnsIn", () => {
  it("notes the first copy that landed after the send, not the same words sent before it", () => {
    resendHello("first-copy");
    const turns = resentTurnsIn("first-copy", [userTurn("before", "hello", SECOND - 5)]);
    expect([...turns.notedTurnIds]).toEqual([]);
    const landed = resentTurnsIn("first-copy", [
      userTurn("before", "hello", SECOND - 5),
      userTurn("first", "hello", SECOND + 1),
    ]);
    expect([...landed.notedTurnIds]).toEqual(["first"]);
    expect([...landed.foldedTurnIds]).toEqual([]);
  });

  it("folds the second copy into the first's line, and only that one", () => {
    resendHello("folding");
    const turns = resentTurnsIn("folding", [
      userTurn("first", "hello", SECOND + 1),
      userTurn("second", "hello", SECOND + 2),
      userTurn("third", "hello", SECOND + 3),
    ]);
    expect([...turns.notedTurnIds]).toEqual(["first"]);
    expect([...turns.foldedTurnIds]).toEqual(["second"]);
  });

  it("does not fold a copy that landed after the chat's next send, which may be the user's own", () => {
    resendHello("closed", SECOND + 2);
    const turns = resentTurnsIn("closed", [
      userTurn("first", "hello", SECOND + 1),
      userTurn("later", "hello", SECOND + 3),
    ]);
    expect([...turns.foldedTurnIds]).toEqual([]);
  });

  it("still folds a second copy that landed before the chat's next send", () => {
    resendHello("before-close", SECOND + 5);
    const turns = resentTurnsIn("before-close", [
      userTurn("first", "hello", SECOND + 1),
      userTurn("second", "hello", SECOND + 3),
    ]);
    expect([...turns.foldedTurnIds]).toEqual(["second"]);
  });

  it("notes a turn with no timestamp, which counts as landing after the send", () => {
    resendHello("no-timestamp");
    const turn = { ...userTurn("first", "hello", SECOND), timestamp: "" };
    expect([...resentTurnsIn("no-timestamp", [turn]).notedTurnIds]).toEqual(["first"]);
  });

  it("notes nothing in a chat with no resent sends", () => {
    expect([...resentTurnsIn("other", [userTurn("first", "hello", SECOND + 1)]).notedTurnIds]).toEqual([]);
  });
});

describe("getResentSendsKey", () => {
  it("changes when a send is added or closed", () => {
    const before = getResentSendsKey("keyed");
    resendHello("keyed");
    const added = getResentSendsKey("keyed");
    resendHello("keyed", SECOND + 1);
    expect(new Set([before, added, getResentSendsKey("keyed")]).size).toBe(3);
  });
});
