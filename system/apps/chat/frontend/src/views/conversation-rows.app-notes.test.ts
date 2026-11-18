// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import m from "mithril";
import type { AssistantMessageEvent, ToolResultEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import type { AppNote } from "../models/AppNotes";
import { buildConversationView, type RowDescriptor } from "./conversation-rows";

// Other apps' notes (models/AppNotes) are placed among a chat's rows by time, so these events carry real times.
const T = (minute: number, second = 0): string =>
  `2026-11-18T10:${String(minute).padStart(2, "0")}:${String(second).padStart(2, "0")}+00:00`;

function userMsg(minute: number, content: string): UserMessageEvent {
  return {
    timestamp: T(minute),
    type: "user_message",
    event_id: `u-${minute}`,
    source: "test",
    role: "user",
    content,
  };
}

function assistantText(minute: number, text: string, stopReason: string | null = null): AssistantMessageEvent {
  return {
    timestamp: T(minute),
    type: "assistant_message",
    event_id: `a-${minute}`,
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

function tkMsg(minute: number, command: string, callId: string): AssistantMessageEvent {
  return {
    ...assistantText(minute, ""),
    event_id: `a-${callId}`,
    tool_calls: [
      {
        tool_call_id: callId,
        tool_name: "Bash",
        input_chars: JSON.stringify({ command }).length,
        tk_command: command,
      },
    ],
  };
}

function result(minute: number, callId: string, output: string): ToolResultEvent {
  return {
    timestamp: T(minute, 1),
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "Bash",
    output_chars: output.length,
    tk_stamp: output,
    is_error: false,
  };
}

function note(id: string, minute: number, second: number, text = "Claude usage 2% (Current session)"): AppNote {
  return { note_id: id, timestamp: T(minute, second), source: "AI Usage Limits", text };
}

function vnodes(
  node: unknown,
): { tag?: unknown; key?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown }[] {
  if (node == null || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(vnodes);
  const v = node as { tag?: unknown; key?: unknown; attrs?: Record<string, unknown>; children?: unknown };
  return [v, ...vnodes(v.children)];
}

function turnParts(row: RowDescriptor): { key?: unknown; children?: unknown }[] {
  const column = vnodes(row.render()).find((v) => v.attrs?.["data-agent-stretch"] !== undefined);
  if (column === undefined) return [];
  return (column.children as { key?: unknown }[]).filter((child) => child.key !== "agent-rail");
}

/** A person's turn by its key, a stretch of the agent's output by the keys of its parts. */
function layout(rows: RowDescriptor[]): (string | string[])[] {
  return rows.map((row) => (row.key.startsWith("turn-") ? turnParts(row).map((part) => String(part.key)) : row.key));
}

function rowsWith(events: TranscriptEvent[], notes: AppNote[]): RowDescriptor[] {
  return buildConversationView("agent-1", events, true, null, null, null, null, notes).rows;
}

describe("other apps' notes among a chat's rows", () => {
  it("puts a note into the agent's stretch just before the first piece newer than it", () => {
    const rows = rowsWith(
      [userMsg(0, "go"), assistantText(1, "working on it"), assistantText(3, "done", "end_turn")],
      [note("n1", 2, 0)],
    );

    expect(layout(rows)).toEqual(["u-0", ["a-1", "note-n1", "a-3"]]);
  });

  it("ends the stretch it arrived in with a note that came before the person's next message", () => {
    const rows = rowsWith(
      [userMsg(0, "go"), assistantText(1, "done", "end_turn"), userMsg(5, "thanks"), assistantText(6, "welcome")],
      [note("n1", 2, 0)],
    );

    expect(layout(rows)).toEqual(["u-0", ["a-1", "note-n1"], "u-5", ["a-6"]]);
  });

  it("puts notes newer than everything held at the end, oldest first whatever order they came in", () => {
    const rows = rowsWith(
      [userMsg(0, "go"), assistantText(1, "done", "end_turn")],
      [note("late", 9, 30), note("early", 9, 0)],
    );

    expect(layout(rows)).toEqual(["u-0", ["a-1", "note-early", "note-late"]]);
    expect(rows[rows.length - 1].takesLiveTail).toBe(true);
  });

  it("gives a note newer than the person's last message a stretch of its own below it", () => {
    const rows = rowsWith([userMsg(0, "go")], [note("n1", 0, 30)]);

    expect(layout(rows)).toEqual(["u-0", ["note-n1"]]);
  });

  it("puts a note that came during a turn with steps after that turn's progress block", () => {
    const rows = rowsWith(
      [
        userMsg(0, "do the thing"),
        tkMsg(1, "tk start cod-step-aaa", "c1"),
        result(1, "c1", "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Look into it"),
        tkMsg(3, 'tk close cod-step-aaa "looked into it"', "c2"),
        result(3, "c2", "Updated cod-step-aaa -> closed\ntk-step cod-step-aaa title: Look into it"),
        assistantText(4, "all done", "end_turn"),
      ],
      [note("n1", 2, 0)],
    );

    expect(layout(rows)).toEqual(["u-0", ["progress-section-u-0", "note-n1"]]);
  });

  it("leaves the rows as they were when there are no notes", () => {
    const events = [userMsg(0, "go"), assistantText(1, "done", "end_turn")];

    expect(layout(rowsWith(events, []))).toEqual(["u-0", ["a-1"]]);
  });

  it("draws a note as a one-line notice naming the app that sent it", () => {
    const rows = rowsWith([userMsg(0, "go"), assistantText(1, "done", "end_turn")], [note("n1", 2, 0)]);
    const part = turnParts(rows[1]).find((p) => p.key === "note-n1");
    expect(part).toBeDefined();

    const container = document.createElement("div");
    m.render(container, part as m.Vnode);

    expect(container.querySelector(".app-note .message-notice")?.textContent?.replace(/\s+/g, " ").trim()).toBe(
      "AI Usage Limits: Claude usage 2% (Current session)",
    );
  });
});
