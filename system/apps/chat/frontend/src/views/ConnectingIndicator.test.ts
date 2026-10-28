// @vitest-environment jsdom
import m from "mithril";
import { describe, expect, it, vi } from "vitest";

vi.mock("../models/Providers", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Providers")>()),
  accountForAgent: (id: string | undefined) => (id === undefined ? null : (accounts.get(id) ?? null)),
  accountForFirstSend: (id: string | undefined) => (id === undefined ? null : (accounts.get(id) ?? null)),
}));
vi.mock("../models/ModelSettings", () => ({
  effectiveChoice: (_chatId: string, live: { matched: { label: string } | null } | null) =>
    live === null ? null : { matched: live.matched },
}));

vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: (id: string) => chats.get(id),
  getProvisionalChat: (id: string) => provisionalChats.get(id),
}));

import type { ChatSnapshot, ProvisionalChat, ProvisionalChatPhase } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { addOutgoing } from "../models/OutgoingMessages";
import { ActivityIndicator } from "./ActivityIndicator";
import { connectingLabel, isChatConnecting } from "./ConnectingIndicator";

const chats = new Map<string, ChatSnapshot>();
const accounts = new Map<string, { provider: string }>([["acct-1", { provider: "Anthropic" }]]);
const provisionalChats = new Map<string, ProvisionalChat>();

function uniqueChatId(): string {
  return `agent-${Math.random().toString(36).slice(2)}`;
}

function provisionalChat(chatId: string, phase: ProvisionalChatPhase): ProvisionalChat {
  return {
    chat_id: chatId,
    name: "Chat 1",
    account_id: "acct-1",
    phase,
    error: null,
    is_seeded: false,
    held_sends: [],
    model_pick: null,
  };
}

// The status row at the end of the transcript, as it reads for a chat with nothing in its transcript yet.
function renderStatusRow(chatId: string): HTMLElement {
  const root = document.createElement("div");
  m.render(root, m(ActivityIndicator, { chatId, events: [] }));
  return root;
}

const CONNECTING_ROW = '.agent-activity-indicator[data-state="CONNECTING"]';

describe("Connecting, on the status row at the end of the transcript", () => {
  it("shows a pulsing warning dot and 'Connecting…' while the backend reports a send waiting on the agent", () => {
    const chatId = uniqueChatId();
    chats.set(chatId, chatSnapshotFixture(chatId, { active_agent: { is_connecting: true } }));

    const root = renderStatusRow(chatId);

    expect(root.querySelector(`${CONNECTING_ROW} .agent-activity-indicator__label`)?.textContent).toBe("Connecting…");
    const dotClass =
      root.querySelector(`${CONNECTING_ROW} .agent-activity-indicator__dot`)?.getAttribute("class") ?? "";
    expect(dotClass).toContain("bg-warning");
    expect(dotClass).toContain("agent-activity-pulse");
  });

  it("shows nothing once the send no longer waits on the agent", () => {
    const chatId = uniqueChatId();
    chats.set(chatId, chatSnapshotFixture(chatId, { active_agent: { is_connecting: false } }));

    expect(isChatConnecting(chatId)).toBe(false);
    expect(renderStatusRow(chatId).querySelector(CONNECTING_ROW)).toBeNull();
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
    expect(
      renderStatusRow(chatId).querySelector(`${CONNECTING_ROW} .agent-activity-indicator__label`)?.textContent,
    ).toBe("Connecting to Anthropic…");
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

  it("names the model and the provider the message waits for, as far as the chat knows them", () => {
    const chatId = uniqueChatId();
    chats.set(
      chatId,
      chatSnapshotFixture(chatId, {
        active_agent: {
          is_connecting: true,
          account_id: "acct-1",
          model_choice: { matched: { label: "Opus 5.5" } } as never,
        },
      }),
    );
    expect(connectingLabel(chatId)).toBe("Connecting to Opus 5.5 on Anthropic…");

    const starting = uniqueChatId();
    provisionalChats.set(starting, { ...provisionalChat(starting, "creating"), held_sends: [] });
    expect(connectingLabel(starting)).toBe("Connecting to Anthropic…");
  });

  it("reads a chat still being created that holds a message from another page as connecting", () => {
    const chatId = uniqueChatId();
    provisionalChats.set(chatId, {
      ...provisionalChat(chatId, "creating"),
      held_sends: [{ message_id: "m-1", text: "hello" }],
    });

    expect(isChatConnecting(chatId)).toBe(true);
  });
});
