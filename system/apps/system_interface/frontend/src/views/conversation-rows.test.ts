import { describe, expect, it } from "vitest";
import m from "mithril";
import type { VirtualItem } from "@tanstack/virtual-core";
import type { TranscriptEvent, ToolResultEvent, AssistantMessageEvent, UserMessageEvent } from "../models/Response";
import { buildConversationRows, isSubagentRunning, renderVirtualRows, type RowDescriptor } from "./conversation-rows";

// --- Event builders (mirroring turn-grouping.test.ts) ---

function userMsg(ts: string, content: string, id = `u-${ts}`): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: id, source: "test", role: "user", content };
}

/** A user_message the backend marked as not to be shown, so it renders no row. */
function hiddenMsg(ts: string): UserMessageEvent {
  return { ...userMsg(ts, "internal"), display: "hidden" };
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
    tool_calls: [{ tool_call_id: callId, tool_name: "Bash", input_preview: JSON.stringify({ command }) }],
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
    output,
    is_error: false,
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
});

/**
 * The event range a row claims is what turns a measured height into reserved
 * scroll space for unloaded history, so these assert the ranges directly. A row
 * covering the wrong events reserves the wrong height for exactly the reason the
 * old per-event constant did.
 */
describe("buildConversationRows event ranges", () => {
  it("covers the loaded window end to end, starting at the window's first offset", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "hello"),
      assistantText("t2", "hi there", "end_turn"),
      userMsg("t3", "again"),
      assistantText("t4", "sure", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true, 500);

    expect(rows.map((r) => [r.start_offset, r.end_offset])).toEqual([
      [500, 501],
      [501, 502],
      [502, 503],
      [503, 504],
    ]);
  });

  it("gives a turn that renders as one progress block that whole turn's events", () => {
    // The collapse the old arithmetic got wrong: four events render as a single
    // ProgressBlock, so the reservation for those four events is that one row's
    // measured height -- not four times a per-event constant.
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

    const rows = buildConversationRows("agent-1", events, true);

    const progressRow = rows.find((r) => r.key.startsWith("progress-"));
    expect(progressRow).toBeDefined();
    // The user message keeps its own single event; everything after it is the turn.
    expect([progressRow?.start_offset, progressRow?.end_offset]).toEqual([1, events.length]);
  });

  it("absorbs an event that renders no row into the row above it", () => {
    // A hole would later be priced at the learned per-event rate, reserving space
    // for content that has none.
    const events: TranscriptEvent[] = [
      userMsg("t1", "hello"),
      assistantText("t2", "hi there", "end_turn"),
      hiddenMsg("t3"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    expect(rows.map((r) => r.key)).toEqual(["u-t1", "a-t2"]);
    expect(rows.map((r) => [r.start_offset, r.end_offset])).toEqual([
      [0, 1],
      [1, 3],
    ]);
  });
});

describe("renderVirtualRows", () => {
  function descriptor(key: string): RowDescriptor {
    return { key, estimate: 100, start_offset: 0, end_offset: 1, render: () => m("div", { key, id: key }) };
  }

  function item(index: number, key: string, start: number, size: number): VirtualItem {
    return { index, key, start, size, end: start + size, lane: 0 };
  }

  /** The heights of the rendered spacers, in order, keyed by their spacer role. */
  function spacers(children: m.Children[]): Array<[string, string]> {
    return children
      .filter((child): child is m.Vnode => typeof child === "object" && child !== null && "key" in child)
      .filter((child) => String(child.key).startsWith("__spacer_"))
      .map((child) => [String(child.key), String((child.attrs as { style: string }).style)]);
  }

  it("brackets the rendered rows with the reserved space above and below them", () => {
    // The leading spacer is the first item's own offset, which already includes
    // whatever was reserved for unloaded history above the window.
    const children = renderVirtualRows([descriptor("a"), descriptor("b")], [item(0, "a", 4000, 100)], 900);

    expect(spacers(children)).toEqual([
      ["__spacer_top", "height: 4000px; overflow-anchor: none"],
      ["__spacer_bottom", "height: 900px; overflow-anchor: none"],
    ]);
  });

  it("bridges a gap between disjoint items with a single spacer", () => {
    // A selection pin holds rows far from the viewport; the rows in between must
    // not be mounted just to fill the space.
    const rows = [descriptor("a"), descriptor("b"), descriptor("c")];
    const children = renderVirtualRows(rows, [item(0, "a", 0, 100), item(2, "c", 5000, 100)], 0);

    expect(spacers(children)).toEqual([
      ["__spacer_top", "height: 0px; overflow-anchor: none"],
      ["__spacer_mid_1", "height: 4900px; overflow-anchor: none"],
      ["__spacer_bottom", "height: 0px; overflow-anchor: none"],
    ]);
  });

  it("has no window to bracket before the first range is computed", () => {
    // The leading spacer is read off items[0], so the empty window (the frame
    // before the scroll element exists) has to be handled ahead of that.
    expect(renderVirtualRows([descriptor("a")], [], 500)).toEqual([]);
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
