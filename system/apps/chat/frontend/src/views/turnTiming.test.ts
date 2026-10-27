import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { formatElapsed, latestTurnSpan } from "./turnTiming";

function userMessage(timestamp: string, display?: string): TranscriptEvent {
  return {
    type: "user_message",
    event_id: `u-${timestamp}`,
    timestamp,
    source: "test",
    role: "user",
    content: "hi",
    ...(display === undefined ? {} : { display }),
  } as TranscriptEvent;
}

function reply(timestamp: string): TranscriptEvent {
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
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

describe("latestTurnSpan", () => {
  it("spans the latest turn from the message that opened it to the agent's last work", () => {
    const span = latestTurnSpan([
      userMessage("2026-10-20T10:00:00Z"),
      reply("2026-10-20T10:00:05Z"),
      userMessage("2026-10-20T10:01:00Z"),
      reply("2026-10-20T10:01:14Z"),
    ]);

    expect(span).toEqual({
      openingEventId: "u-2026-10-20T10:01:00Z",
      startedAt: Date.parse("2026-10-20T10:01:00Z"),
      lastEventAt: Date.parse("2026-10-20T10:01:14Z"),
      lastWorkEventId: "a-2026-10-20T10:01:14Z",
      hasAgentWork: true,
    });
  });

  it("reads a skill expansion inside the turn as part of it, not as the turn's start", () => {
    const span = latestTurnSpan([
      userMessage("2026-10-20T10:00:00Z"),
      userMessage("2026-10-20T10:00:02Z", "skill_expansion"),
      reply("2026-10-20T10:00:09Z"),
    ]);

    expect(span?.startedAt).toBe(Date.parse("2026-10-20T10:00:00Z"));
  });

  it("ends the turn at the agent's last work, not at a compaction that ran once it was idle", () => {
    const span = latestTurnSpan([
      userMessage("2026-10-20T10:00:00Z"),
      reply("2026-10-20T10:00:14Z"),
      userMessage("2026-10-20T10:20:00Z", "status"),
    ]);

    expect(span?.lastEventAt).toBe(Date.parse("2026-10-20T10:00:14Z"));
  });

  it("says when the agent has done nothing in the turn yet", () => {
    expect(latestTurnSpan([userMessage("2026-10-20T10:00:00Z")])).toEqual({
      openingEventId: "u-2026-10-20T10:00:00Z",
      startedAt: Date.parse("2026-10-20T10:00:00Z"),
      lastEventAt: Date.parse("2026-10-20T10:00:00Z"),
      lastWorkEventId: null,
      hasAgentWork: false,
    });
  });

  it("has no span without a turn-opening message, or with one whose time does not parse", () => {
    expect(latestTurnSpan([])).toBeNull();
    expect(latestTurnSpan([reply("2026-10-20T10:00:14Z")])).toBeNull();
    expect(latestTurnSpan([userMessage("not a time"), reply("2026-10-20T10:00:14Z")])).toBeNull();
  });
});

describe("formatElapsed", () => {
  it.each([
    [0, "0s"],
    [999, "0s"],
    [8_400, "8s"],
    [59_999, "59s"],
    [65_000, "1m 05s"],
    [3_599_000, "59m 59s"],
    [3_720_000, "1h 02m"],
    [-2_000, "0s"],
  ])("writes %i ms as %s", (milliseconds, text) => {
    expect(formatElapsed(milliseconds)).toBe(text);
  });
});
