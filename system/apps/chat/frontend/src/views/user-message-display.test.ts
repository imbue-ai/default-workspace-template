// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import m from "mithril";
import type { UserMessageEvent } from "../models/Response";
import { renderUserMessage, StableUserMessage } from "./user-message-display";

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

describe("user-message-display system chips", () => {
  it("draws a system chip without the tool-call part: it is not a tool the agent called", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-stop-hook",
      source: "claude",
      role: "user",
      content: "Stop hook feedback:\nlint failed",
      display: "chip",
      display_label: "Stop hook feedback",
    };
    const root = document.createElement("div");
    m.render(root, renderInner(event));

    expect(root.textContent).toContain("Stop hook feedback");
    expect(root.querySelector('[data-part="chat.tool-call"]')).toBeNull();
  });
});

describe("user-message-display status messages", () => {
  // A compaction renders as chips in the agent's chip rows (compaction-chips.ts), which the
  // transcript walk places; it has no row of its own here.
  it("renders no row for a compaction, with or without a summary", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-status-summary",
      source: "claude",
      role: "system",
      content: "Context was compacted",
      display: "status",
      non_turn_tail: true,
      compaction_cause: "idle",
    };
    expect(renderUserMessage(event)).toBeNull();
    expect(renderUserMessage({ ...event, display_body: "Summary of earlier conversation." })).toBeNull();
  });

  it("renders a typed /compact as the user's own bubble", () => {
    const event: UserMessageEvent = {
      timestamp: "2026-01-01T00:00:00Z",
      type: "user_message",
      event_id: "evt-compact-command",
      source: "claude",
      role: "user",
      content: "/compact",
      non_turn_tail: true,
    };
    expect(collectClasses(renderUserMessage(event))).toContain("message message-user flex flex-col items-end mb-5");
    expect(allText(renderInner(event))).toBe("/compact");
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

describe("user-message-display sent-again line", () => {
  const event: UserMessageEvent = {
    timestamp: "2026-01-01T00:00:00Z",
    type: "user_message",
    event_id: "evt-resent",
    source: "claude",
    role: "user",
    content: "did you get this?",
  };

  function renderRow(isResent: boolean): HTMLElement {
    const root = document.createElement("div");
    m.render(root, renderUserMessage(event, isResent));
    return root;
  }

  it("draws no line under a message sent once", () => {
    expect(renderRow(false).querySelector(".message-resent-note")).toBeNull();
  });

  it("draws one collapsed line under the bubble of a message sent again, which its help opens", () => {
    const collapsed = renderRow(true);
    expect(collapsed.querySelector(".message-user-bubble")?.textContent).toBe("did you get this?");
    const note = collapsed.querySelector(".message-resent-note");
    expect(note?.textContent).toBe("Sent again?");
    const help = collapsed.querySelector<HTMLButtonElement>(".message-resent-note-help");
    expect(help?.getAttribute("aria-expanded")).toBe("false");

    help!.click();
    const opened = renderRow(true);
    expect(opened.querySelector(".message-resent-note-help")?.getAttribute("aria-expanded")).toBe("true");
    expect(opened.querySelector(".message-resent-note-text")?.textContent).toBe(
      "Imbue Studio couldn't confirm the agent received this message, so it sent it again. " +
        "If the first copy arrived too, the agent may see it twice.",
    );

    opened.querySelector<HTMLButtonElement>(".message-resent-note-help")!.click();
    expect(renderRow(true).querySelector(".message-resent-note-text")).toBeNull();
  });
});
