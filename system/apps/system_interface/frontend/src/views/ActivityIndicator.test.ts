import { describe, expect, it } from "vitest";
import type { TranscriptEvent } from "../models/Response";
import { isWorkingActivityState, labelForActivityState } from "./ActivityIndicator";

function userMsg(ts: string): TranscriptEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content: "hi" };
}

function toolUse(ts: string, toolName: string, callId: string, input: string, caption?: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "test-model",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: toolName, input_chars: input.length, caption_label: caption }],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function toolResult(ts: string, callId: string): TranscriptEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "test-tool",
    output_chars: "result".length,
    is_error: false,
  };
}

describe("labelForActivityState — fixed-label states", () => {
  it("hides the indicator for null state (server has no activity tracking for this agent)", () => {
    expect(labelForActivityState(null, [])).toBe(null);
  });

  it("hides the indicator for undefined state (pre-WS-connect)", () => {
    expect(labelForActivityState(undefined, [])).toBe(null);
  });

  it("hides the indicator for IDLE", () => {
    expect(labelForActivityState("IDLE", [userMsg("2026-04-28T01:00:00Z")])).toBe(null);
  });

  it("hides the indicator for an unknown / future state value", () => {
    expect(labelForActivityState("SOMETHING_NEW", [])).toBe(null);
  });

  it("returns 'Thinking…' for THINKING", () => {
    expect(labelForActivityState("THINKING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Thinking…");
  });
});

describe("labelForActivityState — TOOL_RUNNING caption", () => {
  // The caption is whatever the harness's parser put on the call, so this view
  // renders the same way for every harness -- these two cases differ only in the
  // label the backend supplied.
  it("renders the caption the parser attached to a claude tool call", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"src/midnight.ts"}', "Reading midnight.ts"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading midnight.ts");
  });

  it("renders the caption the parser attached to a codex code-mode exec", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "exec", "tc1", 'await tools.exec_command({"cmd":"ls -la"})', "Running ls -la"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running ls -la");
  });

  it("picks the most recent unmatched tool call, skipping resolved ones", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}', "Reading old.ts"),
      toolResult("2026-04-28T01:00:02Z", "tc1"),
      toolUse("2026-04-28T01:00:03Z", "Read", "tc2", '{"file_path":"new.ts"}', "Reading new.ts"),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Reading new.ts");
  });

  it("falls back to 'Running tool…' for a call parsed before labels existed", () => {
    const events = [
      userMsg("2026-04-28T01:00:00Z"),
      toolUse("2026-04-28T01:00:01Z", "Read", "tc1", '{"file_path":"old.ts"}'),
    ];
    expect(labelForActivityState("TOOL_RUNNING", events)).toBe("Running tool…");
  });

  it("falls back to 'Running tool…' when no pending tool call is visible yet (timing race)", () => {
    expect(labelForActivityState("TOOL_RUNNING", [userMsg("2026-04-28T01:00:00Z")])).toBe("Running tool…");
  });
});

describe("isWorkingActivityState — stop-button visibility gate", () => {
  it("treats THINKING / TOOL_RUNNING as an interruptible turn", () => {
    expect(isWorkingActivityState("THINKING")).toBe(true);
    expect(isWorkingActivityState("TOOL_RUNNING")).toBe(true);
  });

  it("treats IDLE as not working (nothing to interrupt)", () => {
    expect(isWorkingActivityState("IDLE")).toBe(false);
  });

  it("treats null / undefined (no activity tracking) as not working", () => {
    expect(isWorkingActivityState(null)).toBe(false);
    expect(isWorkingActivityState(undefined)).toBe(false);
  });

  it("treats an unknown / future state value as not working", () => {
    expect(isWorkingActivityState("SOMETHING_NEW")).toBe(false);
  });
});
