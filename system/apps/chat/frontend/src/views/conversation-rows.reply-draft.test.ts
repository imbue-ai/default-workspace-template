// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it } from "vitest";
import type { AssistantMessageEvent, ToolResultEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { buildConversationView, type ConversationView, type RowDescriptor } from "./conversation-rows";
import { toggleBlockExpanded } from "./expansion-state";

function userMsg(ts: string, content: string): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content };
}

function assistant(ts: string, id: string, text: string, toolCalls: AssistantMessageEvent["tool_calls"]) {
  const event: AssistantMessageEvent = {
    timestamp: ts,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text,
    tool_calls: toolCalls,
    stop_reason: toolCalls.length > 0 ? "tool_use" : null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
  return event;
}

function tkStart(ts: string, id: string, title: string): TranscriptEvent[] {
  const call = { tool_call_id: `c-${ts}`, tool_name: "Bash", input_chars: 20, tk_command: `tk start ${id}` };
  const output = `Updated ${id} -> in_progress\ntk-step ${id} title: ${title}`;
  const toolResult: ToolResultEvent = {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${ts}`,
    source: "test",
    tool_call_id: call.tool_call_id,
    tool_name: "Bash",
    output_chars: output.length,
    tk_stamp: output,
    is_error: false,
  };
  return [assistant(ts, `a-${ts}`, "", [call]), toolResult];
}

function work(ts: string): AssistantMessageEvent {
  return assistant(ts, `a-${ts}`, "", [
    { tool_call_id: `w-${ts}`, tool_name: "Bash", input_chars: 24, action_note: "Look at the disk" },
  ]);
}

const DRAFT = assistant("draft", "reply-draft-agent-1", "Looking inside the cache now.", []);

/** A turn whose step s1 is open, with work done in it. */
const STEP_OPEN_EVENTS: TranscriptEvent[] = [
  userMsg("t0", "go"),
  ...tkStart("t1", "s1", "Find what's using disk space"),
  work("t2"),
];

function renderRow(row: RowDescriptor): HTMLElement {
  const root = document.createElement("div");
  m.render(root, row.render());
  return root;
}

function progressRow(view: ConversationView): RowDescriptor {
  const row = view.rows.find((candidate) => candidate.key.startsWith("progress-"));
  if (row === undefined) throw new Error("the view has no progress row");
  return row;
}

describe("the reply draft", () => {
  it("shows as the live step's caption, where its words will land", () => {
    const view = buildConversationView(
      "agent-1",
      STEP_OPEN_EVENTS,
      /* isTailSettled */ false,
      null,
      null,
      () => DRAFT,
    );

    expect(view.isReplyDraftInStep).toBe(true);
    const caption = renderRow(progressRow(view)).querySelector(".pv-tl-narration");
    expect(caption?.textContent).toContain("Looking inside the cache now.");
  });

  it("follows the live step's work when the step is opened", () => {
    const view = buildConversationView("agent-1", STEP_OPEN_EVENTS, false, null, null, () => DRAFT);
    const block = progressRow(view);
    toggleBlockExpanded(`step:${block.key}:s1`);
    try {
      const expanded = renderRow(block).querySelector(".pv-tl-expanded");
      expect(expanded?.textContent).toContain("Looking inside the cache now.");
    } finally {
      toggleBlockExpanded(`step:${block.key}:s1`);
    }
  });

  it("stays out of the rows when no step is live", () => {
    const events = [userMsg("t0", "go"), work("t1")];
    const view = buildConversationView("agent-1", events, false, null, null, () => DRAFT);

    expect(view.isReplyDraftInStep).toBe(false);
    for (const row of view.rows) {
      expect(renderRow(row).textContent).not.toContain("Looking inside the cache now.");
    }
  });

  it("stays out of a settled turn's open step", () => {
    const view = buildConversationView("agent-1", STEP_OPEN_EVENTS, /* isTailSettled */ true, null, null, () => DRAFT);

    expect(view.isReplyDraftInStep).toBe(false);
    expect(renderRow(progressRow(view)).textContent).not.toContain("Looking inside the cache now.");
  });
});
