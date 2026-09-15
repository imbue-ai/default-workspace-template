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

const ROOT = () => document.getElementById("root") as HTMLElement;

function chatWith(handoff: ChatSnapshot["handoff"]): ChatSnapshot {
  return chatSnapshotFixture("agent-a", { active_agent: { harness: "claude" }, handoff });
}

describe("the handoff node's words", () => {
  it("reads as done once the switch has landed, whatever the snapshot says", () => {
    const node: HandoffNode = { key: "u-req", request: REQUEST, events: [WRITE], switch: SWITCH };
    expect(handoffNodeText(node, chatWith(handoffStateFixture()))).toEqual({
      title: "Handed off from Claude to Codex",
      status: "done",
    });
  });

  it("follows the live switch while it runs, fails, or was called off", () => {
    const open: HandoffNode = { key: "u-req", request: REQUEST, events: [], switch: null };
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
});

describe("the handoff node", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    state.chat = chatWith(null);
  });

  it("spins while open, and expands to the summary turn once done", () => {
    state.chat = chatWith(handoffStateFixture({ phase: "summarizing" }));
    const open: HandoffNode = { key: "u-req", request: REQUEST, events: [WRITE], switch: null };
    m.render(ROOT(), renderHandoffNode(open, "agent-a", new Map(), { isLast: true, expansionKey: "k" }));
    expect(ROOT().querySelector('[data-handoff-status="active"]')).not.toBeNull();
    expect(ROOT().querySelector(".spinner")).not.toBeNull();
    expect(ROOT().textContent).toContain("Handing off to Codex…");

    state.chat = chatWith(null);
    const done: HandoffNode = { ...open, switch: SWITCH };
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
