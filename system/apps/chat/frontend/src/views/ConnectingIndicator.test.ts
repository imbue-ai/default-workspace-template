// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it, vi } from "vitest";

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: (id: string) => chats.get(id),
  getProvisionalChat: (id: string) => provisionalChats.get(id),
}));

import type { ChatSnapshot, ProvisionalChat, ProvisionalChatPhase } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { addOutgoing } from "../models/OutgoingMessages";
import { ConnectingIndicator, isChatConnecting } from "./ConnectingIndicator";

const chats = new Map<string, ChatSnapshot>();
const provisionalChats = new Map<string, ProvisionalChat>();

function uniqueChatId(): string {
  return `agent-${Math.random().toString(36).slice(2)}`;
}

function provisionalChat(chatId: string, phase: ProvisionalChatPhase): ProvisionalChat {
  return { chat_id: chatId, name: "Chat 1", account_id: "acct-1", phase, error: null, is_seeded: false };
}

function renderIndicator(chatId: string): HTMLElement {
  const root = document.createElement("div");
  m.render(root, m(ConnectingIndicator, { chatId }));
  return root;
}

describe("the Connecting indicator beside the model bar", () => {
  it("shows a pulsing warning dot and 'Connecting…' while the backend reports a send waiting on the agent", () => {
    const chatId = uniqueChatId();
    chats.set(chatId, chatSnapshotFixture(chatId, { active_agent: { is_connecting: true } }));

    const root = renderIndicator(chatId);

    expect(root.querySelector(".connecting-indicator__label")?.textContent).toBe("Connecting…");
    const dotClass = root.querySelector(".connecting-indicator__dot")?.getAttribute("class") ?? "";
    expect(dotClass).toContain("bg-warning");
    expect(dotClass).toContain("agent-activity-pulse");
  });

  it("shows nothing once the send no longer waits on the agent", () => {
    const chatId = uniqueChatId();
    chats.set(chatId, chatSnapshotFixture(chatId, { active_agent: { is_connecting: false } }));

    expect(isChatConnecting(chatId)).toBe(false);
    expect(renderIndicator(chatId).querySelector(".connecting-indicator")).toBeNull();
  });

  it("leaves a switching chat to the handoff's own progress text", () => {
    const chatId = uniqueChatId();
    chats.set(
      chatId,
      chatSnapshotFixture(chatId, { handoff: handoffStateFixture(), active_agent: { is_connecting: true } }),
    );

    expect(isChatConnecting(chatId)).toBe(false);
  });

  it("reads a message waiting on a chat still being created as connecting", () => {
    const chatId = uniqueChatId();
    provisionalChats.set(chatId, provisionalChat(chatId, "creating"));
    expect(isChatConnecting(chatId)).toBe(false);

    addOutgoing(chatId, "hello");

    expect(isChatConnecting(chatId)).toBe(true);
    expect(renderIndicator(chatId).querySelector(".connecting-indicator__label")?.textContent).toBe("Connecting…");
  });

  it("does not read a chat that has not started creating, or whose create failed, as connecting", () => {
    const awaiting = uniqueChatId();
    provisionalChats.set(awaiting, provisionalChat(awaiting, "awaiting_first_send"));
    addOutgoing(awaiting, "hello");
    const failed = uniqueChatId();
    provisionalChats.set(failed, provisionalChat(failed, "failed"));
    addOutgoing(failed, "hello");

    expect(isChatConnecting(awaiting)).toBe(false);
    expect(isChatConnecting(failed)).toBe(false);
    expect(isChatConnecting(uniqueChatId())).toBe(false);
  });
});
