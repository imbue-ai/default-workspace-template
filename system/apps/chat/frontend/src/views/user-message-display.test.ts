// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { UserMessageEvent } from "../models/Response";
import { renderUserMessage, StableUserMessage } from "./user-message-display";
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
    const children = Array.isArray(inner.children) ? inner.children : [];
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
