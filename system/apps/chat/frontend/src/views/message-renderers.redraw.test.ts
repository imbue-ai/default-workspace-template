// @vitest-environment jsdom
/**
 * The auth-error note must catch up when the chat lands after the transcript.
 *
 * `message-renderers.test.ts` calls the render functions directly, which is exactly the
 * redraw whose absence was the bug: assistant messages go on screen through the memoized
 * `StableAssistantMessage`, and the note inside one reads the chat from the Chats store --
 * a value that arrives over the chat app's WebSocket, after the transcript's own fetch.
 * Nothing about the EVENT moves when it does, so a memo keyed on the event alone holds the
 * first, chat-less paint for good and the switch link never appears.
 *
 * So this file MOUNTS the message (auto-redraw on, like the real app) and never renders by
 * hand.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const store = vi.hoisted(() => ({ chat: undefined as unknown }));
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () => store.chat,
}));
vi.mock("../shell", () => ({ openSubagentTab: vi.fn(), startChatOnAccount: vi.fn() }));
// A mounted tree instantiates its components, so this stand-in has to be a real one
// (the direct-render tests get away with a bare function).
vi.mock("../markdown", () => ({ MarkdownContent: { view: () => null } }));
vi.mock("./SwitchDialog", () => ({ beginSwitchToAccountId: vi.fn() }));

import m from "mithril";

import type { AssistantMessageEvent, ToolResultEvent } from "../models/Response";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { closeProviderChooser, getUnpickableAccount } from "../models/Providers";
import { StableAssistantMessage } from "./message-renderers";

const ACCOUNT_ID = "acct-openai";

function authErrorEvent(): AssistantMessageEvent {
  return {
    timestamp: "2026-08-06T00:00:00.000Z",
    type: "assistant_message",
    event_id: "err-1",
    source: "test",
    model: "<synthetic>",
    text: "Login expired -- please run /login",
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: true,
    is_api_error: true,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

/** Mithril redraws on an animation frame; give it one, plus a microtask tick. */
async function settle(): Promise<void> {
  await new Promise((resolve) => {
    requestAnimationFrame(() => resolve(undefined));
  });
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function switchLink(): HTMLElement | null {
  const buttons = [...document.querySelectorAll<HTMLElement>("button")];
  return buttons.find((b) => b.textContent?.trim() === "switch to another provider") ?? null;
}

beforeEach(() => {
  closeProviderChooser();
  const previous = document.getElementById("root");
  if (previous !== null) m.mount(previous, null);
  document.body.innerHTML = '<div id="root"></div>';
  // The transcript's first paint, before the chat list has arrived. The event and its result
  // map are built ONCE and handed back on every redraw, as the stores hand back the same
  // objects for an untouched message -- a fresh object each pass would defeat the memo under
  // test and let these pass against the bug.
  store.chat = undefined;
  const event = authErrorEvent();
  const toolResults = new Map<string, ToolResultEvent>();
  m.mount(document.getElementById("root") as HTMLElement, {
    view: () => m(StableAssistantMessage, { event, toolResults, chatId: "chat-1" }),
  });
});

describe("the auth-error note without a hand-cranked render", () => {
  it("offers the switch link once the chat list lands", async () => {
    expect(switchLink()).toBeNull();

    store.chat = chatSnapshotFixture("chat-1", { active_agent: { harness: "codex", account_id: ACCOUNT_ID } });
    m.redraw();
    await settle();

    expect(switchLink()).not.toBeNull();
  });

  it("refuses the account the chat moved to, not the one it started on", async () => {
    store.chat = chatSnapshotFixture("chat-1", { active_agent: { harness: "codex", account_id: ACCOUNT_ID } });
    m.redraw();
    await settle();

    // A rebind lands: the chooser this note opens has to refuse the NEW account.
    store.chat = chatSnapshotFixture("chat-1", { active_agent: { harness: "codex", account_id: "acct-other" } });
    m.redraw();
    await settle();

    switchLink()?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await settle();

    expect(getUnpickableAccount()).toEqual({ accountId: "acct-other", note: "Not working", isFailing: true });
  });
});
