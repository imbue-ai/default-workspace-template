import { describe, expect, it } from "vitest";
import type {
  TranscriptEvent,
  ToolResultEvent,
  AgentSwitchEvent,
  AssistantMessageEvent,
  UserMessageEvent,
} from "../models/Response";
import {
  buildConversationRows,
  isSubagentRunning,
  ESTIMATED_ASSISTANT_HEIGHT_PX,
  ESTIMATED_CHIP_ROW_HEIGHT_PX,
} from "./conversation-rows";

// --- Event builders (mirroring turn-grouping.test.ts) ---

function userMsg(ts: string, content: string, id = `u-${ts}`): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: id, source: "test", role: "user", content };
}

function assistantText(ts: string, text: string, stopReason: string | null = null): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${ts}`,
    source: "test",
    model: "m",
    text,
    tool_calls: [],
    stop_reason: stopReason,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

/** A tk lifecycle Bash call as it appears in the transcript. */
function tkMsg(ts: string, command: string, callId: string): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [
      {
        tool_call_id: callId,
        tool_name: "Bash",
        input_chars: JSON.stringify({ command }).length,
        tk_command: command,
      },
    ],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function result(callId: string, output: string): ToolResultEvent {
  return {
    timestamp: `${callId}-r`,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "Bash",
    output_chars: output.length,
    // The backend stamps the tk-relevant lines resident; these fixtures' outputs ARE
    // decoration lines, so the stamp carries them verbatim.
    tk_stamp: output,
    is_error: false,
  };
}

/** A plain (non-tk) tool call -- the kind that renders as a chip. */
function toolMsg(ts: string, callId: string, note: string): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_chars: 24, action_note: note }],
    stop_reason: "tool_use",
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

/** A delegation call, which renders as its own sub-agent card rather than a chip. */
function agentMsg(ts: string, callId: string, description: string): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${callId}`,
    source: "test",
    model: "m",
    text: "",
    tool_calls: [{ tool_call_id: callId, tool_name: "Agent", input_chars: 24, description }],
    stop_reason: "tool_use",
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

describe("buildConversationRows", () => {
  // The point of the shared builder: a subagent's transcript runs the same
  // section -> rows pipeline as the main chat, so a turn that declares tk steps
  // renders as a single ProgressBlock (the timeline), not raw tk Bash calls.
  it("renders a turn with tk steps as one progress block", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "do the thing"),
      tkMsg("t2", "tk start cod-step-aaa", "c1"),
      result("c1", "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Look into it"),
      tkMsg("t3", 'tk close cod-step-aaa "looked into it"', "c2"),
      result(
        "c2",
        "Updated cod-step-aaa -> closed\ntk-step cod-step-aaa title: Look into it\ntk-step cod-step-aaa summary: looked into it",
      ),
    ];

    const rows = buildConversationRows("agent-1", events, /* agentIsIdle */ true);

    const userRow = rows.find((r) => r.key === "u-t1");
    expect(userRow).toBeDefined();
    const progressRows = rows.filter((r) => r.key.startsWith("progress-"));
    expect(progressRows).toHaveLength(1);
    // The raw tk Bash calls are folded into the progress block, not surfaced as
    // their own rows.
    expect(rows.some((r) => r.key === "a-c1" || r.key === "a-c2")).toBe(false);
  });

  it("renders a turn with no steps as plain user/assistant rows", () => {
    const events: TranscriptEvent[] = [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn")];

    const rows = buildConversationRows("agent-1", events, true);

    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-t2"]);
    expect(rows.some((r) => r.key.startsWith("progress-"))).toBe(false);
  });

  // A harness emits one event per model response, so back-to-back tool calls
  // arrive as separate events. They have to land on ONE row, or each renders a
  // lone chip a full message gap below the last.
  it("merges a run of consecutive tool-call events into one chip row", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      toolMsg("t2", "c1", "Show the current date"),
      toolMsg("t3", "c2", "Check the app is running"),
      assistantText("t4", "that is it", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // One row for both calls, standing where the first of them does, and the
    // wrap-up reply below it.
    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-c1", "a-t4"]);
    const chipRow = rows.find((r) => r.key === "a-c1")!;
    expect(chipRow.estimate).toBe(ESTIMATED_CHIP_ROW_HEIGHT_PX);
    expect(chipRow.anchorEventId).toBe("a-c1");
  });

  // The line of intent and the calls that carry it out are one message the
  // harness happened to split, so they share a row and the chips sit tucked
  // under the sentence rather than a full message gap below it.
  it("tucks a chip run onto the prose that introduces it", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      assistantText("t2", "Checking how much disk and memory this workspace is using."),
      toolMsg("t3", "c1", "Check disk usage"),
      toolMsg("t4", "c2", "Check disk and memory"),
      assistantText("t5", "plenty of room", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // The prose and both calls are one row, standing where the prose does; the
    // result stays its own message below.
    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-t2", "a-t5"]);
    expect(rows.find((r) => r.key === "a-t2")!.estimate).toBe(ESTIMATED_ASSISTANT_HEIGHT_PX);
  });

  // Only chip-only events accrete onto a run: prose is a message in its own
  // right, so it ends the run in progress and heads the next one.
  it("ends a chip run at prose, which then heads a run of its own", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      toolMsg("t2", "c1", "Show the current date"),
      toolMsg("t3", "c2", "Check the app is running"),
      assistantText("t4", "halfway there"),
      toolMsg("t5", "c3", "Read the log"),
      assistantText("t6", "done", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // c1+c2 on one chip row, then the mid-turn prose heading a row that takes
    // c3 with it, then the wrap-up reply.
    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-c1", "a-t4", "a-t6"]);
    expect(rows.find((r) => r.key === "a-c1")!.estimate).toBe(ESTIMATED_CHIP_ROW_HEIGHT_PX);
    expect(rows.find((r) => r.key === "a-t4")!.estimate).toBe(ESTIMATED_ASSISTANT_HEIGHT_PX);
  });

  // A sub-agent is a whole conversation rather than an action, so its card is
  // not a chip: it cannot join the chip row above it, and ends that run.
  it("ends a chip run at a sub-agent card", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      toolMsg("t2", "c1", "Show the current date"),
      agentMsg("t3", "c2", "Explore the codebase"),
      toolMsg("t4", "c3", "Read the log"),
      assistantText("t5", "done", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // The card does not fold into c1's chip row; it heads its own, which c3
    // then joins the way it would inside a single event.
    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-c1", "a-c2", "a-t5"]);
    expect(rows.find((r) => r.key === "a-c2")!.estimate).toBe(ESTIMATED_ASSISTANT_HEIGHT_PX);
  });

  // A chat that moved to another agent shows the seam as its own row, the handoff node keyed by
  // the switch event, between the two agents' turns.
  it("renders an agent switch as a handoff node row between the agents' turns", () => {
    const agentSwitch: AgentSwitchEvent = {
      timestamp: "t3",
      type: "agent_switch",
      event_id: "sw1",
      source: "chat",
      agent_id: "agent-b",
      from_agent_id: "agent-a",
      to_agent_id: "agent-b",
      from_harness: "claude",
      to_harness: "codex",
      seq: 1,
      message_id: null,
      message: null,
      is_fresh_start: false,
    };
    const events: TranscriptEvent[] = [
      userMsg("t1", "hello"),
      assistantText("t2", "from claude", "end_turn"),
      agentSwitch,
      assistantText("t4", "from codex", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-t2", "handoff-sw1", "a-t4"]);
    expect(rows[2].anchorEventId).toBe("sw1");

    // A fresh start had no handoff to show: no row stands between the two agents' turns.
    const fresh = buildConversationRows(
      "agent-1",
      [events[0], events[1], { ...agentSwitch, is_fresh_start: true }, events[3]],
      true,
    );
    expect(fresh.map((r) => r.key)).toEqual(["u-t1", "a-t2", "a-t4"]);
  });
});

describe("isSubagentRunning", () => {
  it("is running while the last assistant turn has not terminally stopped", () => {
    expect(isSubagentRunning([assistantText("t1", "working", null)])).toBe(true);
    expect(isSubagentRunning([assistantText("t1", "calling a tool", "tool_use")])).toBe(true);
  });

  it("is settled once the last assistant turn stops", () => {
    expect(isSubagentRunning([assistantText("t1", "done", "end_turn")])).toBe(false);
  });

  it("is not running with no assistant turns", () => {
    expect(isSubagentRunning([userMsg("t1", "hi")])).toBe(false);
  });
});
