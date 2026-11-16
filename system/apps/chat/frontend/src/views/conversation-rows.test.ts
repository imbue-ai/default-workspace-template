// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import m from "mithril";
import type {
  TranscriptEvent,
  ToolResultEvent,
  AgentSwitchEvent,
  AssistantMessageEvent,
  CompactionCause,
  UserMessageEvent,
} from "../models/Response";
import {
  buildConversationRows,
  isSubagentRunning,
  ESTIMATED_ASSISTANT_HEIGHT_PX,
  ESTIMATED_CHIP_ROW_HEIGHT_PX,
  type RowDescriptor,
} from "./conversation-rows";
import { AutocompactNotice } from "./AutocompactNotice";
import { ToolChipGroup, type ChipEntry } from "./ToolChipGroup";

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

/** Every node of a rendered (unmounted) vnode tree. */
function vnodes(
  node: unknown,
): { tag?: unknown; key?: unknown; attrs?: Record<string, unknown>; children?: unknown }[] {
  if (node == null || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(vnodes);
  const v = node as { tag?: unknown; key?: unknown; attrs?: Record<string, unknown>; children?: unknown };
  return [v, ...vnodes(v.children)];
}

/** The parts a stretch's row holds down its column, as the vnodes the row renders (the line over them aside). */
function turnParts(row: RowDescriptor): { key?: unknown; children?: unknown }[] {
  const column = vnodes(row.render()).find((v) => v.attrs?.["data-agent-stretch"] !== undefined);
  if (column === undefined) return [];
  return (column.children as { key?: unknown }[]).filter((child) => child.key !== "agent-rail");
}

/** The transcript as the list shows it: a person's turn by its key, a stretch of the agent's output by the keys of
 *  its parts. */
function layout(rows: RowDescriptor[]): (string | string[])[] {
  return rows.map((row) => (row.key.startsWith("turn-") ? turnParts(row).map((part) => String(part.key)) : row.key));
}

/** The part of a stretch with this key, wherever it sits. */
function part(rows: RowDescriptor[], key: string): unknown {
  const found = rows.flatMap(turnParts).find((p) => p.key === key);
  expect(found).toBeDefined();
  return found;
}

const NOTICE: UserMessageEvent = {
  ...userMsg("t5", "<task-notification>\n<status>completed</status>\n</task-notification>", "n1"),
  display: "notice",
  display_label: "Background task completed",
  display_body: 'Background command "Wait for the worker" completed',
};

const AGENT_SWITCH: AgentSwitchEvent = {
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

    const rows = buildConversationRows("agent-1", events, /* isTailSettled */ true);

    // The raw tk Bash calls are folded into the progress block, not surfaced as parts of their own.
    expect(layout(rows)).toEqual(["u-t1", ["progress-section-u-t1"]]);
  });

  it("renders a turn with no steps as a person's row and the agent's stretch", () => {
    const events: TranscriptEvent[] = [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn")];

    const rows = buildConversationRows("agent-1", events, true);

    expect(layout(rows)).toEqual(["u-t1", ["a-t2"]]);
    expect(rows[1].anchorEventId).toBe("a-t2");
  });

  // A notice the agent was woken by sits between what it said before and what it did after, in the same stretch.
  it("renders a notice between the turn it ended and the work it woke the agent for", () => {
    const plain = buildConversationRows(
      "agent-1",
      [userMsg("t1", "hello"), assistantText("t2", "on it", "end_turn"), NOTICE, assistantText("t6", "it finished")],
      true,
    );
    expect(layout(plain)).toEqual(["u-t1", ["a-t2", "n1", "a-t6"]]);

    const withStep = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "do the thing"),
        tkMsg("t2", "tk start cod-step-aaa", "c1"),
        result("c1", "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Look into it"),
        assistantText("t3", "running in the background", "end_turn"),
        NOTICE,
        tkMsg("t6", 'tk close cod-step-aaa "looked into it"', "c2"),
        result("c2", "Updated cod-step-aaa -> closed\ntk-step cod-step-aaa title: Look into it"),
      ],
      true,
    );
    expect(layout(withStep)).toEqual(["u-t1", ["progress-section-u-t1", "n1", "progress-section-n1"]]);
  });

  // Back-to-back tool calls arrive as separate events; as separate parts each
  // would render as a lone chip a full message gap below the last.
  it("merges a run of consecutive tool-call events into one chip row", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      toolMsg("t2", "c1", "Show the current date"),
      toolMsg("t3", "c2", "Check the app is running"),
      assistantText("t4", "that is it", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // One part for both calls, standing where the first of them does, and the
    // wrap-up reply below it.
    expect(layout(rows)).toEqual(["u-t1", ["a-c1", "a-t4"]]);
    expect(rows[1].estimate).toBe(ESTIMATED_CHIP_ROW_HEIGHT_PX + ESTIMATED_ASSISTANT_HEIGHT_PX);
    expect(rows[1].anchorEventId).toBe("a-c1");
  });

  // The line of intent and the calls that carry it out are one message, so the
  // chips sit tucked under the sentence rather than a message gap below it.
  it("tucks a chip run onto the prose that introduces it", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "go"),
      assistantText("t2", "Checking how much disk and memory this workspace is using."),
      toolMsg("t3", "c1", "Check disk usage"),
      toolMsg("t4", "c2", "Check disk and memory"),
      assistantText("t5", "plenty of room", "end_turn"),
    ];

    const rows = buildConversationRows("agent-1", events, true);

    // The prose and both calls are one part, standing where the prose does; the
    // result stays its own message below.
    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "a-t5"]]);
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

    // c1+c2 on one chip run, then the mid-turn prose heading a run that takes
    // c3 with it, then the wrap-up reply.
    expect(layout(rows)).toEqual(["u-t1", ["a-c1", "a-t4", "a-t6"]]);
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

    // The card does not fold into c1's chip run; it heads its own, which c3
    // then joins the way it would inside a single event.
    expect(layout(rows)).toEqual(["u-t1", ["a-c1", "a-c2", "a-t5"]]);
  });

  // A chat that moved to another agent shows the seam at the end of the retiring agent's stretch, the handoff node
  // keyed by the switch event, and the new agent's output is a stretch of its own.
  it("ends the retiring agent's stretch at its handoff node and starts the new agent's", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "hello"),
      assistantText("t2", "from claude", "end_turn"),
      AGENT_SWITCH,
      assistantText("t4", "from codex", "end_turn"),
    ];

    expect(layout(buildConversationRows("agent-1", events, true))).toEqual([
      "u-t1",
      ["a-t2", "handoff-sw1"],
      ["a-t4"],
    ]);

    // A fresh start had no handoff to show, though the two agents' output is still two stretches.
    const fresh = buildConversationRows(
      "agent-1",
      [events[0], events[1], { ...AGENT_SWITCH, is_fresh_start: true }, events[3]],
      true,
    );
    expect(layout(fresh)).toEqual(["u-t1", ["a-t2"], ["a-t4"]]);
  });
});

describe("a stretch of the agent's output", () => {
  const STEP_DONE = "Updated cod-step-aaa -> closed\ntk-step cod-step-aaa title: Install the update";

  /** Each row mounted on its own, as the windowed list mounts it. */
  function renderRows(rows: RowDescriptor[]): HTMLElement {
    const list = document.createElement("div");
    for (const row of rows) {
      const slot = document.createElement("div");
      m.render(slot, row.render());
      list.appendChild(slot);
    }
    return list;
  }

  /** The pieces on a stretch's line, by the kind each one is met as, in order. */
  function railKinds(stretch: Element): string[] {
    return [...stretch.querySelectorAll("[data-rail-node]")].map((node) => node.getAttribute("data-rail-node") ?? "");
  }

  it("draws each message's prose as a bubble of its own under one header, with its tool work between them", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "does this version move the avatar?"),
        assistantText("t2", "I'll check whether this version moves it."),
        toolMsg("t3", "c1", "Search the new version's history"),
        assistantText("t4", "The new version does move it.", "end_turn"),
      ],
      true,
    );

    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "a-t4"]]);
    const list = renderRows(rows);
    expect(list.querySelectorAll("[data-machine-header]")).toHaveLength(1);
    const bubbles = [...list.querySelectorAll("[data-machine-bubble]")];
    expect(bubbles.map((bubble) => bubble.textContent?.trim())).toEqual([
      "I'll check whether this version moves it.",
      "The new version does move it.",
    ]);
    // Only the stretch's first bubble squares its corner toward the face.
    expect(bubbles.map((bubble) => bubble.className.includes("rounded-tl-[4px]"))).toEqual([true, false]);
    expect(list.querySelector("[data-machine-bubble] .tool-chip-group")).toBeNull();
    expect(railKinds(list.querySelector("[data-agent-stretch]")!)).toEqual(["box", "chips", "box"]);
  });

  it("starts a new stretch once the person speaks, keeping a system notice on the line of the stretch it lands in", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "install it"),
        assistantText("t2", "Waiting on the background agent.", "end_turn"),
        NOTICE,
        assistantText("t6", "It finished; installing now.", "end_turn"),
        userMsg("t7", "thanks"),
        assistantText("t8", "Any time.", "end_turn"),
      ],
      true,
    );

    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "n1", "a-t6"], "u-t7", ["a-t8"]]);
    const list = renderRows(rows);
    expect(list.querySelectorAll("[data-machine-header]")).toHaveLength(2);
    const stretches = list.querySelectorAll("[data-agent-stretch]");
    expect(railKinds(stretches[0])).toEqual(["box", "point", "box"]);
    expect(stretches[0].querySelector("[data-machine-bubble] .message-notice")).toBeNull();
  });

  // Claude records a lone "\n\n" text block as its own line before the tool call's line lands.
  it("draws no bubble for a whitespace-only line the agent wrote before its tool call landed", () => {
    const before = [userMsg("t1", "install it"), assistantText("t2", "\n\n")];
    expect(layout(buildConversationRows("agent-1", before, false))).toEqual(["u-t1"]);
    expect(layout(buildConversationRows("agent-1", [...before, toolMsg("t3", "c1", "Install")], false))).toEqual([
      "u-t1",
      ["a-c1"],
    ]);
  });

  it("stands a bash-mode command and its output on the person's side, ending the agent's stretch", () => {
    const bashInput: UserMessageEvent = {
      ...userMsg("t3", "<bash-input>ls</bash-input>", "b1"),
      display: "person_chip",
      display_label: "Bash",
      display_body: "ls",
    };
    const bashOutput: UserMessageEvent = {
      ...userMsg("t4", "<bash-stdout>notes.txt</bash-stdout>", "b2"),
      display: "person_chip",
      display_label: "Output",
      display_body: "notes.txt",
    };
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "hello"),
        assistantText("t2", "hi", "end_turn"),
        bashInput,
        bashOutput,
        assistantText("t5", "I see notes.txt.", "end_turn"),
      ],
      true,
    );

    expect(layout(rows)).toEqual(["u-t1", ["a-t2"], "b1", "b2", ["a-t5"]]);
    expect(renderRows(rows).querySelectorAll("[data-agent-stretch] .message-system-collapsed")).toHaveLength(0);
  });

  it("names a run of the person's turns once: the turn just above is the one a turn carries on", () => {
    const rows = buildConversationRows(
      "agent-1",
      [userMsg("t1", "one"), userMsg("t2", "two"), assistantText("t3", "ok", "end_turn"), userMsg("t4", "three")],
      true,
    );

    expect(layout(rows)).toEqual(["u-t1", "u-t2", ["a-t3"], "u-t4"]);
    const list = renderRows(rows);
    // The viewer's own turns: "You" over the first of a run, and again once the agent has spoken between.
    expect(
      [...list.querySelectorAll(".message-user")].map((row) => row.querySelector(".message-you") !== null),
    ).toEqual([true, false, true]);
    expect(rows.map((row) => row.speakerTurn?.event_id ?? null)).toEqual(["u-t1", "u-t2", null, "u-t4"]);
  });

  it("puts the agent's output past the rows at the end of the last stretch only while that stretch is open", () => {
    const tail = (): m.Children[] => [m("div.live-tail", { key: "live" }, "still writing")];
    const open = buildConversationRows(
      "agent-1",
      [userMsg("t1", "hello"), assistantText("t2", "working on it")],
      true,
      null,
      null,
      tail,
    );
    expect(open[open.length - 1].takesLiveTail).toBe(true);
    expect(layout(open)).toEqual(["u-t1", ["a-t2", "live"]]);

    // Another agent took over and has written nothing yet: the reply it is writing is not the retiring agent's.
    const switched = buildConversationRows(
      "agent-1",
      [userMsg("t1", "hello"), assistantText("t2", "from claude", "end_turn"), AGENT_SWITCH],
      true,
      null,
      null,
      tail,
    );
    expect(layout(switched)).toEqual(["u-t1", ["a-t2", "handoff-sw1"]]);
    expect(switched[switched.length - 1].takesLiveTail).toBe(false);

    // The transcript ends on the person's turn: the agent's output past it is a stretch of its own, drawn by the page.
    const afterPerson = buildConversationRows(
      "agent-1",
      [userMsg("t1", "hello"), assistantText("t2", "hi", "end_turn"), userMsg("t3", "and?")],
      true,
      null,
      null,
      tail,
    );
    expect(afterPerson[afterPerson.length - 1].takesLiveTail).toBe(false);
  });

  it("lays a turn's timeline, its prose and its closing reply on one line, the prose in bubbles of its own", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "install the update"),
        assistantText("t2", "Checks all passed. Installing now."),
        tkMsg("t3", 'tk close cod-step-aaa "Installed the update"', "c1"),
        result("c1", STEP_DONE),
        toolMsg("t4", "c2", "Notify the user and show this chat"),
        assistantText("t5", "The script moved in the update."),
        toolMsg("t6", "c3", "Send the finished notification"),
        assistantText("t7", "Your workspace is on the new version.", "end_turn"),
      ],
      true,
    );

    expect(layout(rows)).toEqual(["u-t1", ["progress-section-u-t1"]]);
    const list = renderRows(rows);
    expect(list.querySelectorAll("[data-machine-header]")).toHaveLength(1);
    const bubbles = [...list.querySelectorAll("[data-machine-bubble]")];
    expect(bubbles.map((bubble) => bubble.textContent?.trim())).toEqual([
      "Checks all passed. Installing now.",
      "The script moved in the update.",
      "Your workspace is on the new version.",
    ]);
    expect(bubbles[0].className).toContain("rounded-tl-[4px]");
    expect(bubbles[2].closest(".pv-final")).not.toBeNull();
    expect(railKinds(list.querySelector("[data-agent-stretch]")!)).toEqual([
      "box",
      "step",
      "chips",
      "box",
      "chips",
      "box",
    ]);
  });

  it("records the calls its line shows as chips, and not the work inside a step", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "install the update"),
        tkMsg("t2", "tk start cod-step-aaa", "c1"),
        result("c1", "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Install the update"),
        toolMsg("t3", "c2", "Run the installer"),
        // A permission and a secret request lifted out of the step: the cards are not chips, the call beside them is.
        {
          ...toolMsg("t3b", "c6", "Read the settings"),
          tool_calls: [
            { tool_call_id: "c6", tool_name: "Bash", input_chars: 24, action_note: "Read the settings" },
            { tool_call_id: "c7", tool_name: "Bash", input_chars: 96, display: "permission_request" },
            { tool_call_id: "c8", tool_name: "Bash", input_chars: 96, display: "secret_request" },
          ],
        },
        tkMsg("t4", 'tk close cod-step-aaa "Installed"', "c3"),
        result("c3", STEP_DONE),
        toolMsg("t5", "c4", "Notify the user"),
        agentMsg("t6", "c5", "Check the install"),
      ],
      false,
    );

    expect([...rows[rows.length - 1].chipCallIds]).toEqual(["c6", "c4"]);
  });
});

describe("a message the chat app sent again", () => {
  function hasClass(node: unknown, className: string): boolean {
    if (node == null || typeof node !== "object") return false;
    if (Array.isArray(node)) return node.some((child) => hasClass(child, className));
    const vnode = node as { attrs?: { className?: unknown; class?: unknown }; children?: unknown };
    const classes = `${String(vnode.attrs?.className ?? "")} ${String(vnode.attrs?.class ?? "")}`.split(/\s+/);
    return classes.includes(className) || hasClass(vnode.children, className);
  }

  it("carries the sent-again line on its first copy and shows no second bubble for the second", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "did you get this?", "copy-1"),
      assistantText("t2", "Yes.", "end_turn"),
      userMsg("t3", "did you get this?", "copy-2"),
      assistantText("t4", "Yes, again.", "end_turn"),
    ];
    const rows = buildConversationRows("chat-resent", events, true, null, {
      notedTurnIds: new Set(["copy-1"]),
      foldedTurnIds: new Set(["copy-2"]),
    });
    expect(layout(rows)).toEqual(["copy-1", ["a-t2", "a-t4"]]);
    expect(hasClass(rows[0].render(), "message-resent-note")).toBe(true);
  });

  it("draws no line without a resent send", () => {
    const rows = buildConversationRows("chat-resent", [userMsg("t1", "hello", "only")], true);
    expect(hasClass(rows[0].render(), "message-resent-note")).toBe(false);
  });
});

describe("a compaction's chips in the rows", () => {
  function compacted(ts: string, id: string, cause: CompactionCause | null): UserMessageEvent {
    return {
      ...userMsg(ts, "Context was compacted", id),
      role: "system",
      display: "status",
      non_turn_tail: true,
      display_body: "The conversation so far, summarized.",
      compaction_cause: cause,
    };
  }

  /** A `/compact` as Claude Code records it, typed by the user or sent by the idle sweep, and the
   *  expansion it repeats it in after the compaction. */
  function compactCommand(ts: string): UserMessageEvent[] {
    return [
      { ...userMsg(ts, "/compact", "u-compact"), non_turn_tail: true },
      { ...userMsg(ts, "/compact", "u-compact-expansion"), display: "hidden", non_turn_tail: true },
    ];
  }

  /** Each chip row a part renders, as its chips. */
  function partChips(rows: RowDescriptor[], key: string): ChipEntry[][] {
    return vnodes(part(rows, key))
      .filter((v) => v.tag === ToolChipGroup)
      .map((v) => v.attrs!.chips as ChipEntry[]);
  }

  /** Each chip row a part renders, as its chips: a call by its id, a status chip by its label. */
  function chipRows(rows: RowDescriptor[], key: string): string[][] {
    return partChips(rows, key).map((chips) => chips.map((c) => (c.kind === "tool" ? c.call.tool_call_id : c.label)));
  }

  function hasNotice(rows: RowDescriptor[], key: string): boolean {
    return vnodes(part(rows, key)).some((v) => v.tag === AutocompactNotice);
  }

  // idle: the sweep compacted after the turn's reply, so the chips follow it.
  it("puts an idle compaction's chips on a chip row of their own under the last turn's reply", () => {
    const rows = buildConversationRows(
      "agent-1",
      [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn"), compacted("t3", "cs1", "idle")],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "cs1"]]);
    expect(rows[1].estimate).toBe(ESTIMATED_ASSISTANT_HEIGHT_PX + ESTIMATED_CHIP_ROW_HEIGHT_PX);
    expect(chipRows(rows, "cs1")).toEqual([["Compacting while idle…", "Compacted"]]);
    expect((part(rows, "cs1") as { attrs: { id?: string } }).attrs.id).toBe("cs1");
  });

  // native: the agent's own compaction joins the chip run it landed in, between its tool calls.
  it("puts a native compaction's chips in the chip run it landed in", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "go"),
        toolMsg("t2", "c1", "Read the log"),
        compacted("t3", "cs-native", "native"),
        toolMsg("t4", "c2", "Check the app is running"),
        assistantText("t5", "done", "end_turn"),
      ],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-c1", "a-t5"]]);
    expect(chipRows(rows, "a-c1")).toEqual([["c1", "Compacting to free up context…", "Compacted", "c2"]]);
  });

  // With no chips before it, a compaction starts a chip row of its own, which the calls after it join.
  it("starts a chip row of its own under prose, and the calls after it join it", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "go"),
        assistantText("t2", "Reading the log first."),
        compacted("t3", "cs-native", "native"),
        toolMsg("t4", "c2", "Read the log"),
        assistantText("t5", "done", "end_turn"),
      ],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "a-t5"]]);
    expect(chipRows(rows, "a-t2")).toEqual([["Compacting to free up context…", "Compacted", "c2"]]);

    // At the head of a turn's stream, the chip row is a part of its own.
    const leading = buildConversationRows(
      "agent-1",
      [userMsg("t1", "go"), compacted("t2", "cs-lead", "native"), assistantText("t3", "done", "end_turn")],
      true,
    );
    expect(layout(leading)).toEqual(["u-t1", ["cs-lead", "a-t3"]]);
  });

  // manual: the typed /compact is the user's own bubble, and the chips stand under it.
  it("shows the typed /compact as a bubble and the chips under it", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "hello"),
        assistantText("t2", "hi there", "end_turn"),
        ...compactCommand("t3"),
        compacted("t4", "cs-manual", "manual"),
      ],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-t2"], "u-compact", ["cs-manual"]]);
    expect(chipRows(rows, "cs-manual")).toEqual([["Compacting as requested…", "Compacted"]]);
  });

  it("puts the start chip alone where the pair will land while the agent compacts, then the pair", () => {
    const before = [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn"), ...compactCommand("t3")];
    const during = buildConversationRows("agent-1", before, true, { cause: "manual" });
    expect(layout(during)).toEqual(["u-t1", ["a-t2"], "u-compact", ["compaction-running"]]);
    expect(chipRows(during, "compaction-running")).toEqual([["Compacting as requested…"]]);
    // It has no event of its own yet, so its row resolves by the one before it.
    expect(during[during.length - 1].anchorEventId).toBeNull();

    // The marker can outlast the event by a poll: the event's own pair shows, and no second start chip.
    const landed = buildConversationRows("agent-1", [...before, compacted("t4", "cs-manual", "manual")], true, {
      cause: "manual",
    });
    expect(layout(landed)).toEqual(["u-t1", ["a-t2"], "u-compact", ["cs-manual"]]);
    expect(chipRows(landed, "cs-manual")).toEqual([["Compacting as requested…", "Compacted"]]);
  });

  // idle in a Claude chat: the sweep's /compact is recorded like a typed one, but the user never typed it.
  it("shows the idle sweep's /compact on the start chip, not as a bubble, while it runs and once it lands", () => {
    const before = [userMsg("t1", "hello"), assistantText("t2", "hi there", "end_turn"), ...compactCommand("t3")];
    const during = buildConversationRows("agent-1", before, true, { cause: "idle" });
    expect(layout(during)).toEqual(["u-t1", ["a-t2", "compaction-running"]]);
    expect(chipRows(during, "compaction-running")).toEqual([["Compacting while idle…"]]);

    const landed = buildConversationRows("agent-1", [...before, compacted("t4", "cs-idle", "idle")], true);
    expect(layout(landed)).toEqual(["u-t1", ["a-t2", "cs-idle"]]);
    expect(chipRows(landed, "cs-idle")).toEqual([["Compacting while idle…", "Compacted"]]);
    const commands = partChips(landed, "cs-idle")
      .flat()
      .flatMap((c) => (c.kind === "status" ? c.sections : []))
      .filter((section) => section.kind === "output" && section.marker === "compaction-command");
    expect(commands).toEqual([{ kind: "output", marker: "compaction-command", text: "/compact" }]);
  });

  it("appends the idle sweep's chips to the last chip run when the turn ended without a reply", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "go"),
        toolMsg("t2", "c1", "Read the log"),
        ...compactCommand("t3"),
        compacted("t4", "cs1", "idle"),
      ],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-c1"]]);
    expect(chipRows(rows, "a-c1")).toEqual([["c1", "Compacting while idle…", "Compacted"]]);
  });

  it("keeps a /compact as a bubble when a message comes between it and the idle compaction, or the compaction running after it is not idle", () => {
    const refused = buildConversationRows(
      "agent-1",
      [...compactCommand("t1"), userMsg("t2", "never mind"), compacted("t3", "cs-idle", "idle")],
      true,
    );
    expect(layout(refused)).toEqual(["u-compact", "u-t2", ["cs-idle"]]);

    const pending = buildConversationRows("agent-1", compactCommand("t1"), true, { cause: "manual" });
    expect(layout(pending)).toEqual(["u-compact", ["compaction-running"]]);
  });

  it("puts the one-time notice under the chip row holding the latest idle compaction only", () => {
    const rows = buildConversationRows(
      "agent-1",
      [
        userMsg("t1", "hello"),
        assistantText("t2", "hi there", "end_turn"),
        compacted("t3", "cs1", "idle"),
        userMsg("t4", "more"),
        toolMsg("t5", "c1", "Read the log"),
        compacted("t6", "cs2", "idle"),
        userMsg("t7", "again"),
        assistantText("t8", "still here", "end_turn"),
        compacted("t9", "cs3", "native"),
      ],
      true,
    );
    expect(layout(rows)).toEqual(["u-t1", ["a-t2", "cs1"], "u-t4", ["a-c1"], "u-t7", ["a-t8", "cs3"]]);
    expect(hasNotice(rows, "cs1")).toBe(false);
    // The latest idle compaction joined the turn's last chip run; the notice sits under that run.
    expect(chipRows(rows, "a-c1")).toEqual([["c1", "Compacting while idle…", "Compacted"]]);
    expect(hasNotice(rows, "a-c1")).toBe(true);
    expect(hasNotice(rows, "cs3")).toBe(false);
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
