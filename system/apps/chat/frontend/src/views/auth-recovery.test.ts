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

  it("hides nothing for the same words typed again after the user wrote something else", () => {
    const other = typed("other", "never mind", "2026-10-28T00:40:00.000Z");
    const otherAnswer = reply("other-answer", "2026-10-28T00:40:05.000Z");
    const muchLater = typed("much-later", "what is 2+2?", "2026-10-28T09:00:00.000Z");
    const laterAnswer = reply("later-answer", "2026-10-28T09:00:05.000Z");

    expect(computeAuthErrorHiddenEventIds([ASKED, REFUSED, other, otherAnswer, muchLater, laterAnswer])).toEqual(
      new Set(),
    );
  });

  it("hides a refused prompt of machine context alone once the chat app has sent it again", () => {
    const contextOnly = (eventId: string, timestamp: string): UserMessageEvent => ({
      ...typed(eventId, "<context>task finished</context>", timestamp),
      display: "prompt_with_context",
      display_body: "",
    });
    const reports = contextOnly("reports", ASKED.timestamp);

    expect(computeAuthErrorHiddenEventIds([reports, REFUSED])).toEqual(new Set());
    expect(computeAuthErrorHiddenEventIds([reports, REFUSED, contextOnly("resent", COPY.timestamp)])).toEqual(
      new Set(["reports", "refused"]),
    );
  });

  it("counts copies the agent answered one by one, and none after the user's own message", () => {
    const second = typed("second", "and 3+3?", "2026-10-28T00:39:55.600Z");
    const other = typed("other", "never mind", "2026-10-28T00:40:20.000Z");
    const secondLater = typed("second-later", "and 3+3?", "2026-10-28T00:41:00.000Z");

    expect(computeAuthErrorHiddenEventIds([ASKED, second, REFUSED, COPY, ANSWER, other, secondLater])).toEqual(
      new Set(["asked"]),
    );
  });
});

describe("messagesToSendAgain", () => {
  it("offers what the chat's last failure refused", () => {
    expect(ids(messagesToSendAgain([REFUSED, ASKED], REFUSED) ?? [])).toEqual(["asked"]);
  });

  it("offers nothing once the user has sent something after the failure", () => {
    expect(messagesToSendAgain([ASKED, REFUSED, COPY], REFUSED)).toBeNull();
  });

  it("offers a message typed again after an earlier failure once", () => {
    const retyped = typed("retyped", "what is 2+2?", "2026-10-28T00:39:58.000Z");
    const refusedAgain = reply("refused-again", "2026-10-28T00:39:58.200Z", true);

    expect(ids(messagesToSendAgain([ASKED, REFUSED, retyped, refusedAgain], refusedAgain) ?? [])).toEqual(["asked"]);
  });

  it("offers nothing once the agent has replied since", () => {
    expect(messagesToSendAgain([ASKED, REFUSED, reply("later", "2026-10-28T00:41:00.000Z")], REFUSED)).toBeNull();
  });

  describe("after a resend that stopped partway", () => {
    const SECOND = typed("second", "and 3+3?", "2026-10-28T00:39:55.600Z");

    it.each<[string, TranscriptEvent[]]>([
      ["before the agent answers the copy", [ASKED, SECOND, REFUSED, COPY]],
      ["once the agent has answered the copy", [ASKED, SECOND, REFUSED, COPY, ANSWER]],
    ])("offers the refused messages that have no copy yet, %s", (_case, events) => {
      expect(ids(messagesToSendAgain(events, REFUSED) ?? [])).toEqual(["second"]);
    });

    it("offers nothing once the user has written something of their own", () => {
      const other = typed("other", "never mind", "2026-10-28T00:41:00.000Z");

      expect(messagesToSendAgain([ASKED, SECOND, REFUSED, COPY, ANSWER, other], REFUSED)).toBeNull();
    });

    it("offers nothing once the user wrote something of their own, even if they later typed the rest again", () => {
      const other = typed("other", "never mind", "2026-10-28T00:41:00.000Z");
      const secondLater = typed("second-later", "and 3+3?", "2026-10-28T00:42:00.000Z");

      expect(messagesToSendAgain([ASKED, SECOND, REFUSED, COPY, ANSWER, other, secondLater], REFUSED)).toBeNull();
    });

    it("offers nothing once a sign-in failed again after it", () => {
      const refusedAgain = reply("refused-again", "2026-10-28T00:40:13.000Z", true);

      expect(messagesToSendAgain([ASKED, SECOND, REFUSED, COPY, refusedAgain], REFUSED)).toBeNull();
    });
  });
});
