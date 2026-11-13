import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const chats = vi.hoisted(() => {
  // Appending events redraws, and mithril schedules a redraw on an animation frame.
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    queued: [] as unknown[],
    handoffHeldSends: null as unknown[] | null,
    provisionalHeldSends: null as unknown[] | null,
  };
});

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getQueuedMessagesForChat: () => chats.queued,
  getChatById: () =>
    chats.handoffHeldSends === null ? undefined : { handoff: { held_sends: chats.handoffHeldSends } },
  getProvisionalChat: () =>
    chats.provisionalHeldSends === null ? undefined : { held_sends: chats.provisionalHeldSends },
}));

import type { AssistantMessageEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { SEED_SOURCE, appendEvents } from "../models/Response";
import { addOutgoing, dropOutgoing } from "../models/OutgoingMessages";
import { registerReplySender, unregisterReplySender } from "./reply-senders";
import { repliesFor, replyStateSignature } from "./reply-state";

let chatCounter = 0;
let chatId = "";

function assistant(eventId: string, text: string, source = "claude"): AssistantMessageEvent {
  return {
    timestamp: "2026-11-01T00:00:00Z",
    type: "assistant_message",
    event_id: eventId,
    source,
    model: "m",
    text,
    tool_calls: [],
    stop_reason: "end_turn",
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function user(eventId: string, content: string, extra: Partial<UserMessageEvent> = {}): UserMessageEvent {
  return {
    timestamp: "2026-11-01T00:00:01Z",
    type: "user_message",
    event_id: eventId,
    source: "claude",
    role: "user",
    content,
    ...extra,
  };
}

const OFFER = "Want me to look you up? [Yes](#reply=yes) [No](#reply=no)";

describe("repliesFor", () => {
  beforeEach(() => {
    chatCounter += 1;
    chatId = `chat-${chatCounter}`;
    chats.queued = [];
    chats.handoffHeldSends = null;
    chats.provisionalHeldSends = null;
  });

  function load(events: TranscriptEvent[]): void {
    appendEvents(chatId, events);
  }

  it("offers nothing for a message with no reply links", () => {
    const plain = assistant("a1", "Just text, and [a link](https://example.com/).");
    load([plain]);
    expect(repliesFor(plain, chatId)).toBeUndefined();
    expect(replyStateSignature(plain, chatId)).toBe("");
  });

  it("is open on the newest message, and answered once the user's turn follows it", () => {
    const offer = assistant("a1", OFFER);
    load([user("u0", "hi"), offer]);
    expect(repliesFor(offer, chatId)).toEqual({ chatId, isAnswered: false });

    load([user("u1", "no")]);
    expect(repliesFor(offer, chatId)).toEqual({ chatId, isAnswered: true });
  });

  it("is answered by a typed turn as by a clicked one, and only by a turn of the user's", () => {
    const offer = assistant("a1", OFFER);
    load([
      offer,
      user("u-chip", "Stop hook feedback", { display: "chip" }),
      user("u-hidden", "framework injection", { display: "hidden" }),
      assistant("a2", "Still here."),
    ]);
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(false);

    load([user("u-context", "context\n\nyes", { display: "prompt_with_context", display_body: "yes" })]);
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(true);
  });

  it("keeps a seeded welcome's replies open until the first send: the seed's own turns answer nothing", () => {
    const welcome = assistant("seed-a1", OFFER, SEED_SOURCE);
    load([user("seed-u0", "an earlier turn of the seed", { source: SEED_SOURCE }), welcome]);
    expect(repliesFor(welcome, chatId)?.isAnswered).toBe(false);
    // The seed may hold a turn of the user's after the welcome as well, written before anyone could answer.
    load([user("seed-u1", "seed turn after", { source: SEED_SOURCE })]);
    expect(repliesFor(welcome, chatId)?.isAnswered).toBe(false);
  });

  it("reads as answered while a message of the user's is on its way, and open again if it does not go", () => {
    const offer = assistant("a1", OFFER);
    load([offer]);
    const outgoing = addOutgoing(chatId, "yes");
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(true);
    dropOutgoing(chatId, outgoing);
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(false);

    chats.queued = [{ queued_id: "q1", content: "yes", timestamp: "t" }];
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(true);
    chats.queued = [];
    chats.handoffHeldSends = [{ message_id: "m1", text: "yes" }];
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(true);
    chats.handoffHeldSends = [];
    chats.provisionalHeldSends = [{ message_id: "m1", text: "yes" }];
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(true);
    chats.provisionalHeldSends = [];
    expect(repliesFor(offer, chatId)?.isAnswered).toBe(false);
  });

  it("reads a message the chat's transcript does not hold (a draft, a sub-agent's) as answered", () => {
    load([assistant("a1", "earlier")]);
    expect(repliesFor(assistant("draft", OFFER), chatId)?.isAnswered).toBe(true);
  });

  describe("signature", () => {
    const owner = {};
    afterEach(() => unregisterReplySender(chatId, owner));

    it("changes when the chat gains a composer to send through, or the message is answered", () => {
      const offer = assistant("a1", OFFER);
      load([offer]);
      const withoutComposer = replyStateSignature(offer, chatId);
      registerReplySender(chatId, owner, () => true);
      const withComposer = replyStateSignature(offer, chatId);
      load([user("u1", "yes")]);
      const answered = replyStateSignature(offer, chatId);
      expect(new Set([withoutComposer, withComposer, answered]).size).toBe(3);
    });
  });
});
