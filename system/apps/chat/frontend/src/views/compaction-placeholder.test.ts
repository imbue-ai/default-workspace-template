// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { CompactionStatusPresentation } from "../models/ChatSettings";
import type { QueuedMessage } from "../models/Chats";
import type { TranscriptEvent } from "../models/Response";

// The placeholder reads the agent's server-derived state through the chats model; the mock
// factory is hoisted, so the state it serves lives in a mutable holder.
const agentState = vi.hoisted(() => ({
  activity_state: null as string | null,
  queued_messages: [] as QueuedMessage[],
}));
vi.mock("../models/Chats", () => ({
  getChatById: () => ({ active_agent: agentState, handoff: null }),
  getQueuedMessagesForChat: () => agentState.queued_messages,
}));

import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests, setChatSettingsForTests } from "../models/ChatSettings";
import { newestCompactionPillId, renderCompactionPlaceholder } from "./compaction-placeholder";

const QUEUED: QueuedMessage = { queued_id: "q1", content: "and then this", timestamp: "2026-10-06T12:00:01Z" };

function pill(timestamp: string, id = `pill-${timestamp}`): TranscriptEvent {
  return {
    timestamp,
    type: "user_message",
    event_id: id,
    source: "claude",
    role: "system",
    content: "Context was compacted",
    display: "status",
    non_turn_tail: true,
  };
}

function prompt(timestamp: string): TranscriptEvent {
  return {
    timestamp,
    type: "user_message",
    event_id: `u-${timestamp}`,
    source: "claude",
    role: "user",
    content: "hi",
  };
}

function usePresentation(presentation: CompactionStatusPresentation): void {
  setChatSettingsForTests({ ...DEFAULT_CHAT_SETTINGS, compaction_status_presentation: presentation });
}

type PlaceholderVnode = m.Vnode<{ chatId: string; events: TranscriptEvent[] }>;

/** Mount the placeholder the panel would render (its component created now), and a function
 *  that renders it again against `events`. */
function mount(events: TranscriptEvent[]): ((next: TranscriptEvent[]) => m.Vnode | null) | null {
  const vnode = renderCompactionPlaceholder("chat-1", events) as PlaceholderVnode | null;
  if (vnode === null) return null;
  const component = (vnode.tag as (initial: PlaceholderVnode) => m.Component<PlaceholderVnode["attrs"]>)(vnode);
  return (next) => component.view({ ...vnode, attrs: { ...vnode.attrs, events: next } } as never) as m.Vnode | null;
}

function allText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(allText).join("");
  if (typeof node === "object") {
    const v = node as { text?: unknown; children?: unknown };
    return (typeof v.text === "string" ? v.text : "") + allText(v.children);
  }
  return "";
}

describe("renderCompactionPlaceholder", () => {
  beforeEach(() => {
    resetChatSettingsForTests();
    vi.spyOn(m, "redraw").mockImplementation(() => undefined);
    agentState.activity_state = "COMPACTING";
    agentState.queued_messages = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetChatSettingsForTests();
  });

  it("holds the conversation's last slot while compacting, under both presentations", () => {
    usePresentation("both");
    const render = mount([prompt("2026-10-06T11:59:00Z")]);
    expect(allText(render?.([prompt("2026-10-06T11:59:00Z")]))).toBe("Compacting…");
  });

  it("is shown under the placeholder presentation and not under the strip one", () => {
    usePresentation("placeholder");
    expect(mount([])).not.toBeNull();
    usePresentation("strip");
    expect(mount([])).toBeNull();
  });

  it("says a queued message waits on the compaction", () => {
    usePresentation("both");
    agentState.queued_messages = [QUEUED];
    expect(allText(mount([])?.([]))).toBe("Compacting, then replying…");
  });

  it("is gone once the agent leaves COMPACTING, and never there for another state", () => {
    usePresentation("both");
    for (const state of ["THINKING", "TOOL_RUNNING", "IDLE", null]) {
      agentState.activity_state = state;
      expect(mount([])).toBeNull();
    }
  });

  it("gives the slot to the pill as soon as this compaction's pill lands", () => {
    usePresentation("both");
    // An earlier compaction's pill is already on the transcript, and history loaded later can
    // add older ones still: neither is this compaction's.
    const earlier = [pill("2026-10-06T10:00:00Z"), prompt("2026-10-06T11:00:00Z")];
    const render = mount(earlier)!;
    expect(render(earlier)).not.toBeNull();
    expect(render([pill("2026-10-06T09:00:00Z"), ...earlier])).not.toBeNull();
    expect(render([...earlier, pill("2026-10-06T12:00:31Z")])).toBeNull();
  });

  // The agent's clock may run behind the browser's, so the new pill can carry any timestamp.
  it("knows its pill by identity, whatever the pill's timestamp", () => {
    usePresentation("both");
    const earlier = [prompt("2026-10-06T11:00:00Z")];
    const render = mount(earlier)!;
    expect(render(earlier)).not.toBeNull();
    expect(render([...earlier, pill("2001-01-01T00:00:00Z")])).toBeNull();
  });
});

describe("newestCompactionPillId", () => {
  it("names the last status pill, skipping other messages after it", () => {
    expect(newestCompactionPillId([pill("t1", "p1"), pill("t2", "p2"), prompt("t3")])).toBe("p2");
    expect(newestCompactionPillId([prompt("t1")])).toBeNull();
    expect(newestCompactionPillId([])).toBeNull();
  });
});
