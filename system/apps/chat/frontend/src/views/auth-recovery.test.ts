import { describe, expect, it } from "vitest";
import type { AssistantMessageEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { computeAuthErrorHiddenEventIds, messagesToSendAgain, refusedTurns } from "./auth-recovery";

function typed(eventId: string, content: string, timestamp: string): UserMessageEvent {
  return { type: "user_message", event_id: eventId, source: "test", role: "user", content, timestamp };
}

function reply(eventId: string, timestamp: string, isAuthError = false): AssistantMessageEvent {
  return {
    type: "assistant_message",
    event_id: eventId,
    source: "test",
    timestamp,
    model: null,
    text: isAuthError ? "Login expired · Please run /login" : "4",
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: isAuthError,
  } as unknown as AssistantMessageEvent;
}

const ASKED = typed("asked", "what is 2+2?", "2026-10-28T00:39:55.530Z");
const REFUSED = reply("refused", "2026-10-28T00:39:55.644Z", true);
const COPY = typed("copy", "what is 2+2?", "2026-10-28T00:40:12.000Z");
const ANSWER = reply("answer", "2026-10-28T00:40:14.000Z");

function ids(events: Iterable<{ event_id: string }>): string[] {
  return [...events].map((event) => event.event_id);
}

describe("refusedTurns", () => {
  it.each<[string, TranscriptEvent[]]>([
    ["after the message", [ASKED, REFUSED]],
    ["ahead of the message it refused", [REFUSED, ASKED]],
  ])("finds the message a failure written %s refused", (_order, events) => {
    const [turn] = refusedTurns(events);

    expect(ids(turn.prompts)).toEqual(["asked"]);
    expect(ids(turn.failures)).toEqual(["refused"]);
    expect(turn.isLast).toBe(true);
    expect(turn.isAnsweredSince).toBe(false);
  });

  it("finds nothing for a turn the agent had worked on before its sign-in failed", () => {
    const worked = reply("worked", "2026-10-28T00:39:56.000Z");

    expect(refusedTurns([ASKED, worked, { ...REFUSED, timestamp: "2026-10-28T00:39:57.000Z" }])).toEqual([]);
  });
});

describe("computeAuthErrorHiddenEventIds", () => {
  it.each<[string, TranscriptEvent[]]>([
    ["after the message", [ASKED, REFUSED, COPY, ANSWER]],
    ["ahead of the message it refused", [REFUSED, ASKED, COPY, ANSWER]],
    ["ahead of the message, before its copy is answered", [REFUSED, ASKED, COPY]],
  ])("hides the refused message and its failure once a copy is sent: failure %s", (_case, events) => {
    expect(computeAuthErrorHiddenEventIds(events)).toEqual(new Set(["asked", "refused"]));
  });

  it("hides nothing while the refused message exists only once", () => {
    expect(computeAuthErrorHiddenEventIds([ASKED, REFUSED, typed("other", "never mind", COPY.timestamp)])).toEqual(
      new Set(),
    );
  });

  it("keeps the failure for a refused message that has no copy yet", () => {
    const second = typed("second", "and 3+3?", "2026-10-28T00:39:55.600Z");

    expect(computeAuthErrorHiddenEventIds([ASKED, second, REFUSED, COPY])).toEqual(new Set(["asked"]));
  });
});

describe("messagesToSendAgain", () => {
  it("offers what the chat's last failure refused", () => {
    expect(ids(messagesToSendAgain([REFUSED, ASKED], REFUSED) ?? [])).toEqual(["asked"]);
  });

  it("offers nothing once the user has sent something after the failure", () => {
    expect(messagesToSendAgain([ASKED, REFUSED, COPY], REFUSED)).toBeNull();
  });

  it("offers nothing once the agent has replied since", () => {
    expect(messagesToSendAgain([ASKED, REFUSED, reply("later", "2026-10-28T00:41:00.000Z")], REFUSED)).toBeNull();
  });
});
