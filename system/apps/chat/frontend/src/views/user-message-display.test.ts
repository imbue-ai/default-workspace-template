// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { CompactionCause, UserMessageEvent } from "../models/Response";
import { AutocompactNotice } from "./AutocompactNotice";
import { compactionWhyKey, renderUserMessage, StableUserMessage } from "./user-message-display";
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";

function collectClasses(node: unknown): string[] {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap(collectClasses);
  if (typeof node === "object") {
    const v = node as { attrs?: { className?: unknown }; children?: unknown };
    const own = typeof v.attrs?.className === "string" ? [v.attrs.className] : [];
    return [...own, ...collectClasses(v.children)];
  }
  return [];
}

function allText(node: unknown): string {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(allText).join("");
  if (typeof node === "object") {
    const v = node as { text?: unknown; children?: unknown };
    const own = typeof v.text === "string" ? v.text : "";
    return own + allText(v.children);
  }
  return "";
}

/** What each `MarkdownContent` in the tree was handed; the bubble delegates its attachment
 *  block to one, which renders as innerHTML and so has no text of its own to collect. */
function markdownContents(node: unknown): string[] {
  if (node == null) return [];
  if (Array.isArray(node)) return node.flatMap(markdownContents);
  if (typeof node === "object") {
    const v = node as { attrs?: { content?: unknown }; children?: unknown };
    const own = typeof v.attrs?.content === "string" ? [v.attrs.content] : [];
    return [...own, ...markdownContents(v.children)];
  }
  return [];
}

function renderInner(event: UserMessageEvent): m.Vnode {
  const comp = StableUserMessage();
  return comp.view(m(StableUserMessage, { event })) as m.Vnode;
}

describe("user-message-display status messages", () => {
  beforeEach(() => {
    setBlockExpanded("status:evt-status-summary", false);
  });

  it("renders a simple status pill when there is no summary body", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-status-plain",
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display: "status",
      non_turn_tail: true,
    };

    const row = renderUserMessage(event);
    expect(row).not.toBeNull();
    expect(collectClasses(row)).toContain("message message-system-status-row");

    const inner = renderInner(event);
    const classes = collectClasses(inner);
    expect(classes).toContain("message-system-status");
    expect(classes).not.toContain("message-system-status--toggleable");
    expect(allText(inner)).toContain("Context was compacted");
  });

  it("renders an expandable toggle and summary details when display_body is present", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-status-summary",
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display_body: "Summary of earlier conversation across 12 turns.",
      display: "status",
      non_turn_tail: true,
    };

    const row = renderUserMessage(event);
    expect(row).not.toBeNull();
    expect(collectClasses(row)).toContain("message message-system-status-row");

    const inner = renderInner(event);
    const classes = collectClasses(inner);
    expect(classes).toContain("message-system-status-container");
    expect(classes).toContain("message-system-status message-system-status--toggleable");
    expect(classes).toContain("tool-call-chevron");
    expect(classes).toContain("message-system-status-details");
    expect(classes).toContain("message-system-status-body");
    expect(allText(inner)).toContain("Context was compacted");
    expect(allText(inner)).toContain("Summary of earlier conversation across 12 turns.");

    // Initial state is collapsed
    expect(classes).not.toContain("message-system-status-container message-system-status-container--expanded");
  });

  it("renders with expanded container class when isBlockExpanded is true", () => {
    setBlockExpanded("status:evt-status-summary", true);
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-status-summary",
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display_body: "Summary of earlier conversation across 12 turns.",
      display: "status",
      non_turn_tail: true,
    };

    const inner = renderInner(event);
    const classes = collectClasses(inner);
    expect(classes).toContain("message-system-status-container message-system-status-container--expanded");
  });

  it("toggles expansion state when clicked", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-status-summary",
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display_body: "Summary text",
      display: "status",
      non_turn_tail: true,
    };

    const inner = renderInner(event);
    // The pill is the first thing on the status line, ahead of its "why?" button.
    const statusLine = (Array.isArray(inner.children) ? inner.children : [])[0] as m.Vnode;
    const children = Array.isArray(statusLine.children) ? statusLine.children : [];
    const toggleChild = children[0] as m.Vnode<{
      onclick?: (e: { currentTarget: HTMLElement }) => void;
      onkeydown?: (e: { key: string; preventDefault: () => void; currentTarget: HTMLElement }) => void;
    }>;
    const containerEl = document.createElement("div");
    containerEl.className = "message-system-status-container";
    const toggleEl = document.createElement("div");
    containerEl.appendChild(toggleEl);

    // Call onclick
    toggleChild.attrs.onclick?.({ currentTarget: toggleEl });
    expect(containerEl.classList.contains("message-system-status-container--expanded")).toBe(true);
    expect(isBlockExpanded("status:evt-status-summary")).toBe(true);

    // Click again to collapse
    toggleChild.attrs.onclick?.({ currentTarget: toggleEl });
    expect(containerEl.classList.contains("message-system-status-container--expanded")).toBe(false);
    expect(isBlockExpanded("status:evt-status-summary")).toBe(false);

    // Keyboard Enter key expands
    let prevented = false;
    toggleChild.attrs.onkeydown?.({
      key: "Enter",
      preventDefault: () => {
        prevented = true;
      },
      currentTarget: toggleEl,
    });
    expect(prevented).toBe(true);
    expect(containerEl.classList.contains("message-system-status-container--expanded")).toBe(true);
    expect(isBlockExpanded("status:evt-status-summary")).toBe(true);
  });
});

describe("user-message-display prompt bubbles", () => {
  const SEED_BLOCK = "<chat-seed-context>\nthe conversation the chat opened on\n</chat-seed-context>";
  const ATTACHMENT = "See attachment here: ![/code/uploads/aaa/diagram.png](/code/uploads/aaa/diagram.png)";

  function wrappedSend(spoken: string): UserMessageEvent {
    return {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-seeded-first-send",
      source: "claude",
      role: "user",
      content: `${SEED_BLOCK}\n${spoken}`,
      display: "prompt_with_context",
      display_body: spoken,
    };
  }

  it("shows only what the user typed when the chat app wrapped the send in context", () => {
    const inner = renderInner(wrappedSend("1"));

    expect(allText(inner)).toBe("1");
    expect(allText(inner)).not.toContain("chat-seed-context");
  });

  it("still renders the attachment a wrapped send carries, beside the words", () => {
    const inner = renderInner(wrappedSend(`here you go\n\n${ATTACHMENT}`));

    expect(allText(inner)).toBe("here you go");
    expect(markdownContents(inner)).toEqual([ATTACHMENT]);
  });
});

describe("user-message-display compaction pill why? popover", () => {
  const EVENT_ID = "evt-compacted";

  function compactionEvent(cause?: CompactionCause | null): UserMessageEvent {
    return {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: EVENT_ID,
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display: "status",
      non_turn_tail: true,
      ...(cause === undefined ? {} : { compaction_cause: cause }),
    };
  }

  type WhyButtonVnode = m.Vnode<{ onclick: () => void; "aria-expanded": string }>;

  /** The "why?" button's vnode (a Button component, so found by the marker in its `extra`). */
  function findWhyButton(node: unknown): WhyButtonVnode | null {
    if (node == null || typeof node !== "object") return null;
    if (Array.isArray(node)) {
      for (const child of node) {
        const found = findWhyButton(child);
        if (found !== null) return found;
      }
      return null;
    }
    const v = node as { attrs?: { extra?: unknown }; children?: unknown };
    if (typeof v.attrs?.extra === "string" && v.attrs.extra.includes("compaction-why-button")) {
      return v as WhyButtonVnode;
    }
    return findWhyButton(v.children);
  }

  beforeEach(() => {
    setBlockExpanded(compactionWhyKey(EVENT_ID), false);
  });

  it.each([
    ["idle", "Compacted while idle to keep replies fast and cheap. Change this under Auto-compact in the model menu."],
    ["manual", "Compacted because you asked (/compact)."],
    ["native", "Your agent triggered compaction. You can ask it about its current setting, or tell it to change it."],
    [null, "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu."],
    [undefined, "Compacted to keep replies fast and cheap. Idle compaction is under Auto-compact in the model menu."],
  ] as const)("opens on why? with the sentence for cause %s, and closes again", (cause, text) => {
    const event = compactionEvent(cause);
    const component = StableUserMessage();
    const vnode = m(StableUserMessage, { event }) as unknown as Parameters<typeof component.view>[0];
    const closed = component.view(vnode);
    expect(allText(closed)).toContain("why?");
    expect(allText(closed)).not.toContain(text);
    expect(findWhyButton(closed)?.attrs["aria-expanded"]).toBe("false");

    findWhyButton(closed)!.attrs.onclick();
    // The memoized row repaints for the toggle even though its event did not change.
    expect(component.onbeforeupdate!.call(component, vnode, vnode as never)).toBe(true);
    const open = component.view(vnode);
    expect(allText(open)).toContain(text);
    expect(collectClasses(open).some((c) => c.includes("compaction-why-popover"))).toBe(true);
    expect(findWhyButton(open)?.attrs["aria-expanded"]).toBe("true");
    expect(component.onbeforeupdate!.call(component, vnode, vnode as never)).toBe(false);

    findWhyButton(open)!.attrs.onclick();
    expect(allText(component.view(vnode))).not.toContain(text);
  });

  it("offers why? beside a pill that also carries a summary", () => {
    const inner = renderInner({ ...compactionEvent("manual"), display_body: "Summary text" });
    expect(findWhyButton(inner)).not.toBeNull();
    expect(collectClasses(inner)).toContain("message-system-status message-system-status--toggleable");
  });

  it("puts the one-time notice under the anchor pill only", () => {
    const hasNotice = (row: m.Vnode | null): boolean =>
      ((row?.children ?? []) as unknown[]).some(
        (child) => (child as { tag?: unknown } | null)?.tag === AutocompactNotice,
      );
    expect(hasNotice(renderUserMessage(compactionEvent("idle"), true))).toBe(true);
    expect(hasNotice(renderUserMessage(compactionEvent("idle")))).toBe(false);
    const prompt: UserMessageEvent = { ...compactionEvent(), display: undefined, content: "hello" };
    expect(hasNotice(renderUserMessage(prompt, true))).toBe(false);
  });
});
