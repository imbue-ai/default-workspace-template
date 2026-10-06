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
}));

import { DEFAULT_CHAT_SETTINGS, resetChatSettingsForTests, updateChatSettings } from "../models/ChatSettings";
import { hasCompactionPillSince, renderCompactionPlaceholder } from "./compaction-placeholder";

const NOW = Date.parse("2026-10-06T12:00:00Z");

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

/** Save the workspace's presentation setting the way the page does, the backend agreeing. */
async function usePresentation(presentation: CompactionStatusPresentation): Promise<void> {
  vi.spyOn(m, "request").mockImplementation((async (options: { body: unknown }) => ({
    settings: options.body,
  })) as never);
  await updateChatSettings({ ...DEFAULT_CHAT_SETTINGS, compaction_status_presentation: presentation });
}

type PlaceholderVnode = m.Vnode<{ chatId: string; events: TranscriptEvent[] }>;

/** Mount the placeholder the panel would render (its component created now), and a function
 *  that renders it again against `events`. */
function mount(events: TranscriptEvent[]): ((next: TranscriptEvent[]) => m.Vnode | null) | null {
  const vnode = renderCompactionPlaceholder("chat-1", events) as PlaceholderVnode | null;
  if (vnode === null) return null;
  const component = (vnode.tag as () => m.Component<PlaceholderVnode["attrs"]>)();
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
    vi.spyOn(Date, "now").mockReturnValue(NOW);
    agentState.activity_state = "COMPACTING";
    agentState.queued_messages = [];
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetChatSettingsForTests();
  });

  it("holds the conversation's last slot while compacting, under both presentations", async () => {
    await usePresentation("both");
    const render = mount([prompt("2026-10-06T11:59:00Z")]);
    expect(allText(render?.([prompt("2026-10-06T11:59:00Z")]))).toBe("Compacting…");
  });

  it("is shown under the placeholder presentation and not under the strip one", async () => {
    await usePresentation("placeholder");
    expect(mount([])).not.toBeNull();
    await usePresentation("strip");
    expect(mount([])).toBeNull();
  });

  it("says a queued message waits on the compaction", async () => {
    await usePresentation("both");
    agentState.queued_messages = [QUEUED];
    expect(allText(mount([])?.([]))).toBe("Compacting, then replying…");
  });

  it("is gone once the agent leaves COMPACTING, and never there for another state", async () => {
    await usePresentation("both");
    for (const state of ["THINKING", "TOOL_RUNNING", "IDLE", null]) {
      agentState.activity_state = state;
      expect(mount([])).toBeNull();
    }
  });

  it("gives the slot to the pill as soon as this compaction's pill lands", async () => {
    await usePresentation("both");
    // An earlier compaction's pill is already on the transcript, and history loaded later can
    // add older ones still: neither is this compaction's.
    const earlier = [pill("2026-10-06T10:00:00Z"), prompt("2026-10-06T11:00:00Z")];
    const render = mount(earlier)!;
    expect(render(earlier)).not.toBeNull();
    expect(render([pill("2026-10-06T09:00:00Z"), ...earlier])).not.toBeNull();
    expect(render([...earlier, pill("2026-10-06T12:00:31Z")])).toBeNull();
  });
});

describe("hasCompactionPillSince", () => {
  it("counts only status pills stamped at or after the time", () => {
    const since = Date.parse("2026-10-06T12:00:00Z");
    expect(hasCompactionPillSince([pill("2026-10-06T11:59:59Z"), prompt("2026-10-06T12:00:05Z")], since)).toBe(false);
    expect(hasCompactionPillSince([pill("2026-10-06T12:00:00Z")], since)).toBe(true);
  });
});
