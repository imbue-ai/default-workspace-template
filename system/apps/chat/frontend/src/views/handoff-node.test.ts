// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return { chat: null as unknown };
});
vi.mock("../models/Chats", () => ({ getChatById: () => state.chat ?? undefined }));

import m from "mithril";
import type { AgentSwitchEvent, AssistantMessageEvent, UserMessageEvent } from "../models/Response";
import { chatSnapshotFixture, handoffStateFixture, rebindStateFixture } from "../models/chatSnapshotFixture";
import type { ChatSnapshot } from "../models/Chats";
import { handoffNodeText, renderHandoffNode, renderHandoffTailNode } from "./handoff-node";
import type { HandoffNode } from "./turn-grouping";

const SWITCH: AgentSwitchEvent = {
  timestamp: "t3",
  type: "agent_switch",
  event_id: "sw1",
  source: "chat",
  from_agent_id: "agent-a",
  to_agent_id: "agent-b",
  from_harness: "claude",
  to_harness: "codex",
  seq: 1,
  message_id: null,
  message: null,
  is_fresh_start: false,
};
const REQUEST: UserMessageEvent = {
  timestamp: "t1",
  type: "user_message",
  event_id: "u-req",
  source: "test",
  role: "user",
  content: "/handoff-summary data/.apps/chat/chats/agent-a/summaries/1.md",
  display: "chip",
  display_label: "Asked for a handoff summary",
};
const WRITE: AssistantMessageEvent = {
  timestamp: "t2",
  type: "assistant_message",
  event_id: "a-write",
  source: "test",
  model: "m",
  text: "",
  tool_calls: [{ tool_call_id: "w1", tool_name: "Write", input_chars: 40 }],
  stop_reason: null,
  usage: null,
  is_auth_error: false,
  is_api_error: false,
  api_error_kind: null,
  is_provider_fault: false,
};

const PROMPT: UserMessageEvent = {
  timestamp: "t4",
  type: "user_message",
  event_id: "u-prompt",
  source: "test",
  role: "user",
  content: 'You are continuing the chat "Chat 1" (chat id agent-a). Now do it in Codex.',
  display: "chip",
  display_label: "Handoff prompt",
};

const ROOT = () => document.getElementById("root") as HTMLElement;

function chatWith(handoff: ChatSnapshot["handoff"]): ChatSnapshot {
  return chatSnapshotFixture("agent-a", { active_agent: { harness: "claude" }, handoff });
}

describe("the handoff node's words", () => {
  it("reads as done once the switch has landed, whatever the snapshot says", () => {
    const node: HandoffNode = { key: "u-req", request: REQUEST, events: [WRITE], switch: SWITCH, prompt: null };
    expect(handoffNodeText(node, chatWith(handoffStateFixture()))).toEqual({
      title: "Handed off from Claude to Codex",
      status: "done",
    });
  });

  it("follows the live switch while it runs, fails, or was called off", () => {
    const open: HandoffNode = { key: "u-req", request: REQUEST, events: [], switch: null, prompt: null };
    expect(handoffNodeText(open, chatWith(handoffStateFixture({ phase: "summarizing" })))).toEqual({
      title: "Handing off to Codex…",
      status: "active",
    });
    expect(handoffNodeText(open, chatWith(handoffStateFixture({ phase: "failed", error: "x" })))).toEqual({
      title: "Could not hand off to Codex",
      status: "failed",
    });
    expect(handoffNodeText(open, chatWith(null))).toEqual({ title: "Handoff called off", status: "cancelled" });
    expect(handoffNodeText(open, chatWith(rebindStateFixture()))).toEqual({
      title: "Restarting Claude on Anthropic 2 (Claude Code)…",
      status: "active",
    });
  });

  it("stays called off when a later switch runs, and reads live only for that switch's own request", () => {
    // Timestamps on a real transcript: the first request predates the second switch's confirmation.
    const earlier: HandoffNode = {
      key: "u-req-1",
      request: { ...REQUEST, timestamp: "2026-09-16T00:43:00.000Z" },
      events: [],
      switch: null,
      prompt: null,
    };
    const later: HandoffNode = {
      key: "u-req-2",
      request: { ...REQUEST, event_id: "u-req-2", timestamp: "2026-09-16T00:44:20.000Z" },
      events: [],
      switch: null,
      prompt: null,
    };
    const second = chatWith(handoffStateFixture({ phase: "summarizing", started_at: "2026-09-16T00:44:10.000Z" }));
    expect(handoffNodeText(earlier, second)).toEqual({ title: "Handoff called off", status: "cancelled" });
    expect(handoffNodeText(later, second)).toEqual({ title: "Handing off to Codex…", status: "active" });
  });
});

describe("the handoff node", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    state.chat = chatWith(null);
  });

  it("spins while open, and expands to the summary turn once done", () => {
    state.chat = chatWith(handoffStateFixture({ phase: "summarizing" }));
    const open: HandoffNode = { key: "u-req", request: REQUEST, events: [WRITE], switch: null, prompt: null };
    m.render(ROOT(), renderHandoffNode(open, "agent-a", new Map(), { isLast: true, expansionKey: "k" }));
    expect(ROOT().querySelector('[data-handoff-status="active"]')).not.toBeNull();
    expect(ROOT().querySelector(".spinner")).not.toBeNull();
    expect(ROOT().textContent).toContain("Handing off to Codex…");
    // No rule while the switch runs: nothing below the node is the successor's yet.
    expect(ROOT().querySelector(".pv-handoff-rule")).toBeNull();

    state.chat = chatWith(null);
    const done: HandoffNode = { ...open, switch: SWITCH, prompt: PROMPT };
    m.render(ROOT(), renderHandoffNode(done, "agent-a", new Map(), { isLast: true, expansionKey: "k" }));
    expect(ROOT().querySelector('[data-handoff-status="done"]')).not.toBeNull();
    expect(ROOT().textContent).toContain("Handed off from Claude to Codex");
    // Collapsed by default; the chevron opens the summary turn: the request chip and the write.
    expect(ROOT().querySelector(".pv-tl-expanded")).toBeNull();
    ROOT().querySelector<HTMLButtonElement>(".pv-tl-title")?.click();
    m.render(ROOT(), renderHandoffNode(done, "agent-a", new Map(), { isLast: true, expansionKey: "k" }));
    expect(ROOT().querySelector(".pv-tl-expanded")).not.toBeNull();
    expect(ROOT().textContent).toContain("Asked for a handoff summary");
    expect(ROOT().textContent).toContain("Write");
    // The successor's prompt closes the expanded body, and the rule marks the segment boundary.
    const chips = Array.from(ROOT().querySelectorAll(".pv-tl-expanded .message-system-collapsed"));
    expect(chips).toHaveLength(2);
    expect(chips[0].textContent).toContain("Asked for a handoff summary");
    expect(chips[1].textContent).toContain("Handoff prompt");
    expect(ROOT().querySelector(".pv-handoff-rule")).not.toBeNull();
  });

  it("expands on the prompt alone for a node the switch made, with no summary request in the window", () => {
    const switchOnly: HandoffNode = { key: "sw1", request: null, events: [], switch: SWITCH, prompt: PROMPT };
    m.render(ROOT(), renderHandoffNode(switchOnly, "agent-a", new Map(), { isLast: true, expansionKey: "k2" }));
    expect(ROOT().querySelector<HTMLButtonElement>(".pv-tl-title")?.disabled).toBe(false);
    ROOT().querySelector<HTMLButtonElement>(".pv-tl-title")?.click();
    m.render(ROOT(), renderHandoffNode(switchOnly, "agent-a", new Map(), { isLast: true, expansionKey: "k2" }));
    expect(ROOT().textContent).toContain("Handoff prompt");
    const bare: HandoffNode = { ...switchOnly, prompt: null };
    m.render(ROOT(), renderHandoffNode(bare, "agent-a", new Map(), { isLast: true, expansionKey: "k3" }));
    expect(ROOT().querySelector<HTMLButtonElement>(".pv-tl-title")?.disabled).toBe(true);
  });

  it("stands in at the tail only while the transcript has no request to show", () => {
    state.chat = chatWith(handoffStateFixture({ phase: "draining" }));
    const tail = renderHandoffTailNode("agent-a", false);
    expect(tail).not.toBeNull();
    m.render(ROOT(), tail);
    expect(ROOT().textContent).toContain("Handing off to Codex…");
    // Once the request is on the stream the rows carry the node; nothing stands in.
    expect(renderHandoffTailNode("agent-a", true)).toBeNull();
    state.chat = chatWith(null);
    expect(renderHandoffTailNode("agent-a", false)).toBeNull();
  });
});
