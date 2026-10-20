// @vitest-environment jsdom
/**
 * The rail's status marks: a chat waiting on a background task wears the dashed ring, a working one the breathing
 * dot (pending tasks or not, since the backend reports it working), and an idle one the hollow ring.
 */
import { afterEach, describe, expect, it } from "vitest";
import m from "mithril";
import { ChatRail } from "./ChatRail";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
import type { ChatRow } from "./rows";

function row(chatId: string, status: string): ChatRow {
  return { chatId, title: chatId, status, labels: {}, agentIds: [chatId], lastActiveMs: null, isProvisional: false };
}

let root: HTMLElement | null = null;

afterEach(() => {
  if (root === null) return;
  m.mount(root, null);
  root.remove();
  root = null;
});

function dotClassOf(chatId: string): string {
  const dot = root?.querySelector(`[data-chat-id="${chatId}"] .chat-rail-dot`);
  if (dot === null || dot === undefined) throw new Error(`no status mark for ${chatId}`);
  return dot.getAttribute("class") ?? "";
}

describe("ChatRail status marks", () => {
  it("draws background as the dashed ring, apart from working and idle", () => {
    root = document.createElement("div");
    document.body.appendChild(root);
    const rows = [row("agent-wait", "background"), row("agent-turn", "working"), row("agent-idle", "idle")];
    m.mount(root, { view: () => m(ChatRail, chatRailAttrsFixture({ rows })) });

    expect(dotClassOf("agent-wait")).toContain("chat-rail-dot--dashed");
    expect(dotClassOf("agent-wait")).not.toContain("chat-rail-dot--pulse");
    expect(dotClassOf("agent-turn")).toContain("chat-rail-dot--pulse");
    expect(dotClassOf("agent-turn")).not.toContain("chat-rail-dot--dashed");
    expect(dotClassOf("agent-idle")).not.toMatch(/chat-rail-dot--(dashed|pulse)/);
    expect(root.querySelector('[data-chat-id="agent-wait"]')?.getAttribute("data-status")).toBe("background");
  });
});
