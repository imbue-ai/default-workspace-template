// @vitest-environment jsdom
/**
 * The chat list's default chat: it leads in its own group, set off from the rest, and its row is marked.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./verbs", () => ({
  stopChat: vi.fn(async (_chatId: string) => undefined),
  startChat: vi.fn(async (_chatId: string) => undefined),
  renameChat: vi.fn(async (_chatId: string, _title: string) => undefined),
  destroyChat: vi.fn(async (_chatId: string) => undefined),
}));

import m from "mithril";
import { scopeOfHandshake } from "@imbue/workspace-ui/src/element_reference";
import { ChatRail } from "./ChatRail";
import { chatRowFixture } from "./chatRowFixture";
import type { ChatRow } from "./rows";

let root: HTMLElement | null = null;

function mount(rows: readonly ChatRow[]): HTMLElement {
  const mounted = document.createElement("div");
  document.body.appendChild(mounted);
  root = mounted;
  m.mount(mounted, {
    view: () =>
      m(ChatRail, {
        rows,
        selectedChatId: null,
        isCompact: false,
        onPick: () => undefined,
        onNew: () => undefined,
        referenceScope: scopeOfHandshake(null),
        onDraftReference: () => undefined,
        isReferenceDraftAvailable: false,
      }),
  });
  return mounted;
}

function chatIdsIn(element: Element): string[] {
  return [...element.querySelectorAll<HTMLElement>(".chat-rail-row")].map((rowElement) => rowElement.dataset.chatId!);
}

afterEach(() => {
  if (root !== null) {
    m.mount(root, null);
    root.remove();
    root = null;
  }
});

describe("ChatRail", () => {
  it("sets the default chat and its helpers apart from the rest, and marks the default row", () => {
    const helper = chatRowFixture("agent-helper", { labels: { agent_created: "true", lead_agent: "agent-welcome" } });
    const rail = mount([chatRowFixture("agent-welcome", { isDefault: true }), helper, chatRowFixture("agent-other")]);

    const group = rail.querySelector(".chat-rail-default-group");
    expect(group).not.toBeNull();
    expect(chatIdsIn(group!)).toEqual(["agent-welcome", "agent-helper"]);
    expect(chatIdsIn(rail)).toEqual(["agent-welcome", "agent-helper", "agent-other"]);
    expect(rail.querySelector('[data-chat-id="agent-welcome"]')?.getAttribute("data-default")).toBe("true");
    expect(rail.querySelector('[data-chat-id="agent-other"]')?.hasAttribute("data-default")).toBe(false);
  });

  it("draws no default group when no chat is the default", () => {
    const rail = mount([chatRowFixture("agent-a"), chatRowFixture("agent-b")]);

    expect(rail.querySelector(".chat-rail-default-group")).toBeNull();
    expect(chatIdsIn(rail)).toEqual(["agent-a", "agent-b"]);
  });
});
