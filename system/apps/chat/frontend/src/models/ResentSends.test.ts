import { describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { request: vi.fn(), redraw: vi.fn() } }));

import { announceMessageSent, appendEvents, getEventsForChat, sentUserTurnIds } from "./Response";
import type { TranscriptEvent } from "./Response";
import { getResentSendsVersion, noteResentSend, resentTurnsIn } from "./ResentSends";

let counter = 0;

function freshChat(): string {
  return `resent-chat-${counter++}`;
}

function userTurn(id: string, content: string): TranscriptEvent {
  return {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: id,
    source: "test",
    message_uuid: id,
    role: "user",
    content,
  };
}

describe("resentTurnsIn", () => {
  it("notes nothing until the resent message's turn lands, then its first copy", () => {
    const chat = freshChat();
    appendEvents(chat, [userTurn("before", "hello")]);
    const versionBefore = getResentSendsVersion(chat);
    noteResentSend(chat, "hello", sentUserTurnIds(chat));
    expect(getResentSendsVersion(chat)).not.toBe(versionBefore);
    expect(resentTurnsIn(chat, getEventsForChat(chat)).notedTurnIds).toEqual(new Set());

    appendEvents(chat, [userTurn("copy-1", "hello")]);
    const turns = resentTurnsIn(chat, getEventsForChat(chat));
    expect(turns.notedTurnIds).toEqual(new Set(["copy-1"]));
    expect(turns.foldedTurnIds).toEqual(new Set());
  });

  it("folds the second copy of a resent message into the first's line", () => {
    const chat = freshChat();
    noteResentSend(chat, "hello", sentUserTurnIds(chat));
    appendEvents(chat, [userTurn("copy-1", "hello"), userTurn("reply-gap", "unrelated"), userTurn("copy-2", "hello")]);
    const turns = resentTurnsIn(chat, getEventsForChat(chat));
    expect(turns.notedTurnIds).toEqual(new Set(["copy-1"]));
    expect(turns.foldedTurnIds).toEqual(new Set(["copy-2"]));
  });

  it("folds only one copy: a third is the user's own", () => {
    const chat = freshChat();
    noteResentSend(chat, "yes", sentUserTurnIds(chat));
    appendEvents(chat, [userTurn("copy-1", "yes"), userTurn("copy-2", "yes"), userTurn("copy-3", "yes")]);
    expect(resentTurnsIn(chat, getEventsForChat(chat)).foldedTurnIds).toEqual(new Set(["copy-2"]));
  });

  it("does not fold a copy that lands after this page sent the chat something else", () => {
    // The user may have sent the same words again themselves.
    const chat = freshChat();
    noteResentSend(chat, "yes", sentUserTurnIds(chat));
    appendEvents(chat, [userTurn("copy-1", "yes")]);
    announceMessageSent(chat);
    appendEvents(chat, [userTurn("later-yes", "yes")]);
    const turns = resentTurnsIn(chat, getEventsForChat(chat));
    expect(turns.notedTurnIds).toEqual(new Set(["copy-1"]));
    expect(turns.foldedTurnIds).toEqual(new Set());
  });

  it("still folds a second copy that had landed before the next send", () => {
    const chat = freshChat();
    noteResentSend(chat, "yes", sentUserTurnIds(chat));
    appendEvents(chat, [userTurn("copy-1", "yes"), userTurn("copy-2", "yes")]);
    announceMessageSent(chat);
    expect(resentTurnsIn(chat, getEventsForChat(chat)).foldedTurnIds).toEqual(new Set(["copy-2"]));
  });

  it("notes nothing in another chat", () => {
    const chat = freshChat();
    const other = freshChat();
    noteResentSend(chat, "hello", sentUserTurnIds(chat));
    appendEvents(other, [userTurn("other-copy", "hello")]);
    expect(resentTurnsIn(other, getEventsForChat(other)).notedTurnIds).toEqual(new Set());
  });
});
