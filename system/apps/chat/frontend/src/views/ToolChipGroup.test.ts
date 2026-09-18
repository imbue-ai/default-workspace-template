// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

// The panel asks the detail cache for on-demand payloads (and kicks off fetches);
// stub those so the tests drive the state machine without mithril's XHR.
const { mockDetailState, mockRequestDetail } = vi.hoisted(() => ({
  mockDetailState: vi.fn(),
  mockRequestDetail: vi.fn(),
}));
vi.mock("../models/Response", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Response")>()),
  getEventDetailState: mockDetailState,
  getEventDetailVersion: () => 0,
  requestEventDetail: mockRequestDetail,
}));

import m from "mithril";
import type { ToolCall, ToolResultEvent } from "../models/Response";
import { setBlockExpanded } from "./expansion-state";
import { ToolChipGroup, type ChipCall } from "./ToolChipGroup";

function chip(call: ToolCall, eventId = "a-1"): ChipCall {
  return { call, eventId };
}

function result(over: Partial<ToolResultEvent> & Pick<ToolResultEvent, "tool_call_id">): ToolResultEvent {
  return {
    timestamp: "t",
    type: "tool_result",
    event_id: `r-${over.tool_call_id}`,
    source: "test",
    tool_name: "Bash",
    output_chars: 0,
    is_error: false,
    ...over,
  };
}

let root: HTMLElement;

function mount(chips: ChipCall[], results: ToolResultEvent[] = []): void {
  const toolResults = new Map(results.map((r) => [r.tool_call_id, r]));
  m.render(root, m(ToolChipGroup, { chips, toolResults, chatId: "agent-x" }));
}

function chipButtons(): HTMLButtonElement[] {
  return [...root.querySelectorAll<HTMLButtonElement>(".tool-chip")];
}

function labels(): string[] {
  return [...root.querySelectorAll(".tool-chip-label")].map((el) => el.textContent ?? "");
}

function detailText(): string {
  return root.querySelector(".tool-chip-detail")?.textContent ?? "";
}

/** Click a chip and re-render, as a redraw would. */
function click(index: number, chips: ChipCall[], results: ToolResultEvent[] = []): void {
  chipButtons()[index].click();
  mount(chips, results);
}

beforeEach(() => {
  mockDetailState.mockReset();
  mockRequestDetail.mockReset();
  root = document.createElement("div");
  document.body.appendChild(root);
});

describe("the tool chip row", () => {
  const read: ToolCall = { tool_call_id: "c1", tool_name: "Read", input_chars: 20, header_label: "Tool: Read" };
  // A real codex code-mode call: tool_name is always "exec"; the operation is buried in
  // the JS input as tools.<fn>(...), and the parser's label is what says what it ran.
  const exec: ToolCall = { tool_call_id: "c2", tool_name: "exec", input_chars: 72, header_label: "Tool: Bash" };
  const unlabelled: ToolCall = { tool_call_id: "c3", tool_name: "Grep", input_chars: 6 };

  it("says what the call did, in two halves", () => {
    const edit: ToolCall = {
      tool_call_id: "c-edit",
      tool_name: "Edit",
      input_chars: 20,
      action_verb: "edited",
      action_target: ".../src/views/timeline-node.ts",
    };
    mount([chip(edit)]);
    // The verb reads as prose and the target as the machine's own text, so they
    // are separate elements rather than one string.
    expect(root.querySelector(".tool-chip-verb")?.textContent).toBe("edited");
    expect(root.querySelector(".tool-chip-target")?.textContent).toBe(".../src/views/timeline-node.ts");
  });

  it("prefers the agent's own words when the tool recorded any", () => {
    const bash: ToolCall = {
      tool_call_id: "c-bash",
      tool_name: "Bash",
      input_chars: 60,
      action_verb: "ran",
      action_target: "rg -n 'font-size' src/style.css",
      action_note: "Sweep the stylesheet for every size",
    };
    mount([chip(bash)]);
    // Nothing beats the agent saying what the command was for, so the note
    // replaces the verb+target rather than joining it.
    expect(labels()).toEqual(["Sweep the stylesheet for every size"]);
    expect(root.querySelector(".tool-chip-target")).toBeNull();
  });

  it("falls back to the live strip's caption for a harness that stamps no action", () => {
    // Every harness stamps `caption_label`; only some stamp the action fields. The
    // caption is the same phrase in the present tense, which reads fine on a chip
    // and beats dropping back to a bare tool name.
    const other: ToolCall = {
      tool_call_id: "c-other",
      tool_name: "exec",
      input_chars: 6,
      caption_label: "Reading foo.py",
    };
    mount([chip(other)]);
    expect(labels()).toEqual(["Reading foo.py"]);
  });

  it("falls back to the tool name for a call parsed before any label existed", () => {
    mount([chip(unlabelled)]);
    expect(labels()).toEqual(["Grep"]);
  });

  it("carries the whole phrase and the tool it came from in the hover title", () => {
    const edit: ToolCall = {
      tool_call_id: "c-title",
      tool_name: "Edit",
      input_chars: 20,
      action_verb: "edited",
      action_target: ".../src/views/timeline-node.ts",
    };
    mount([chip(edit)]);
    // The row truncates a long target, and the chip no longer names its tool
    // anywhere -- the title is where both are recoverable.
    expect(chipButtons()[0].title).toBe("edited .../src/views/timeline-node.ts\nEdit");
  });

  it("shows no detail until a chip is picked", () => {
    mount([chip(read), chip(exec)]);
    expect(root.querySelector(".tool-chip-detail")).toBeNull();
    expect(chipButtons().every((b) => b.getAttribute("aria-pressed") === "false")).toBe(true);
  });

  it("opens one chip at a time, and closes it when picked again", () => {
    mockDetailState.mockReturnValue(undefined);
    const chips = [chip(read), chip(exec)];
    mount(chips);

    click(0, chips);
    expect(chipButtons()[0].getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelectorAll(".tool-chip-detail")).toHaveLength(1);

    // Picking the other one moves the panel rather than opening a second.
    click(1, chips);
    expect(chipButtons()[0].getAttribute("aria-pressed")).toBe("false");
    expect(chipButtons()[1].getAttribute("aria-pressed")).toBe("true");
    expect(root.querySelectorAll(".tool-chip-detail")).toHaveLength(1);

    click(1, chips);
    expect(root.querySelector(".tool-chip-detail")).toBeNull();
  });

  it("puts the panel directly after the chip that opened it, inside the row", () => {
    mockDetailState.mockReturnValue(undefined);
    const chips = [chip(read), chip(exec), chip(unlabelled)];
    mount(chips);
    click(1, chips);

    // A long run wraps onto several lines, so a panel hung below the whole row
    // would sit lines away from its own chip. Being the chip's next sibling --
    // and full-width, which no line can share -- is what puts it directly
    // underneath wherever the chip happens to have wrapped to.
    const panel = root.querySelector(".tool-chip-detail");
    expect(panel?.previousElementSibling).toBe(chipButtons()[1]);
    expect(panel?.parentElement?.className).toContain("tool-chip-row");
  });

  it("keeps a chip open across a remount, so virtualization does not collapse it", () => {
    mockDetailState.mockReturnValue(undefined);
    // Its own call id: the expansion store is module-global and session-scoped
    // (it has to outlive the rows it describes), so a test that leaves a key set
    // would otherwise hand the next one an already-open chip.
    const kept: ToolCall = { tool_call_id: "c-kept", tool_name: "Read", input_chars: 20 };
    setBlockExpanded("chip:c-kept", true);
    mount([chip(kept)]);
    expect(root.querySelector(".tool-chip-detail")).not.toBeNull();
    setBlockExpanded("chip:c-kept", false);
  });

  it("marks a failed call on the chip itself, before anything is opened", () => {
    mount([chip(read), chip(exec)], [result({ tool_call_id: "c2", is_error: true })]);
    expect(chipButtons()[0].className).toContain("text-faint");
    expect(chipButtons()[1].className).toContain("text-danger");
  });
});

describe("the open chip's detail panel", () => {
  const call: ToolCall = { tool_call_id: "pc-1", tool_name: "Bash", input_chars: 4000 };
  const done = result({ tool_call_id: "pc-1", output_chars: 5000 });

  beforeEach(() => {
    setBlockExpanded("chip:pc-1", true);
  });

  it("shows loading notes and requests both payloads while nothing is cached", () => {
    mockDetailState.mockReturnValue(undefined);
    mount([chip(call, "a-pc-1")], [done]);
    expect(detailText()).toContain("Loading");
    expect(mockRequestDetail).toHaveBeenCalledWith("agent-x", "a-pc-1");
    expect(mockRequestDetail).toHaveBeenCalledWith("agent-x", "r-pc-1");
  });

  it("renders the full fetched input and output once loaded", () => {
    mockDetailState.mockImplementation((_chatId: string, eventId: string) =>
      eventId === "a-pc-1"
        ? {
            state: "loaded",
            detail: { inputs_by_tool_call_id: { "pc-1": "the whole input" }, output: null, thinking: null },
          }
        : { state: "loaded", detail: { inputs_by_tool_call_id: {}, output: "the whole output", thinking: null } },
    );
    mount([chip(call, "a-pc-1")], [done]);
    expect(detailText()).toContain("the whole input");
    expect(detailText()).toContain("the whole output");
  });

  it("shows the quiet placeholder when the payload is gone", () => {
    mockDetailState.mockReturnValue({ state: "unavailable" });
    mount([chip(call, "a-pc-1")], [done]);
    expect(detailText()).toContain("No longer available");
  });

  it("leads with a failure's resident snippet, which is readable before any fetch lands", () => {
    mockDetailState.mockReturnValue(undefined);
    const failed = result({
      tool_call_id: "pc-1",
      output_chars: 5000,
      is_error: true,
      error_snippet: "FileNotFoundError: no such file",
    });
    mount([chip(call, "a-pc-1")], [failed]);
    expect(root.querySelector(".tool-call-error-snippet")?.textContent).toContain("FileNotFoundError: no such file");
    // Still loading the real output, and the snippet did not wait for it.
    expect(detailText()).toContain("Loading");
  });
});
