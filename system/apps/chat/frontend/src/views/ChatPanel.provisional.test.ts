// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type m from "mithril";

// vi.mock factories are hoisted above module scope, so anything they close over must come from
// vi.hoisted. Mithril captures requestAnimationFrame at import time, and jsdom has none.
const mocks = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    proto: null as unknown,
    // The chat the list names, once its agent exists; undefined while it is provisional.
    chat: undefined as unknown,
    chatsUpdatedListener: null as (() => void) | null,
    launchChat: vi.fn(async (_chatId: string, _accountId: string) => ({})),
    fetchEvents: vi.fn(async (_chatId: string) => undefined),
    loadSnapshotWithStream: vi.fn(async (_chatId: string) => undefined),
    connectToStream: vi.fn(),
    noteLoadedArrivals: vi.fn(),
    // The page's not-yet-real bubbles, as the outgoing view renders them.
    outgoingBubbles: [] as unknown[],
    // Whether the transcript load 404'd, and whether the chat app has sent its chat list yet.
    isConversationNotFound: false,
    isChatListReceived: true,
  };
});

vi.mock("../models/Chats", () => ({
  getChatById: () => mocks.chat,
  getProvisionalChat: () => mocks.proto,
  hasReceivedChatList: () => mocks.isChatListReceived,
  launchChat: (chatId: string, accountId: string) => mocks.launchChat(chatId, accountId),
  addChatsUpdatedListener: (listener: () => void) => {
    mocks.chatsUpdatedListener = listener;
  },
  removeChatsUpdatedListener: () => undefined,
  buildAgentTerminalUrl: () => "",
  getTerminalUrl: () => "",
}));
vi.mock("../models/Response", () => ({
  addMessageSentListener: () => undefined,
  removeMessageSentListener: () => undefined,
  evictEvents: () => undefined,
  fetchBackfillEvents: async () => undefined,
  fetchEvents: (chatId: string) => mocks.fetchEvents(chatId),
  fetchForwardEvents: async () => undefined,
  fetchWindowAtOffset: async () => undefined,
  getConversationLoadState: () => ({ phase: "idle", error: null }),
  getEventsForChat: () => [],
  getEventCount: () => 0,
  getFirstOffset: () => 0,
  getRenderVersion: () => 0,
  getTotalEventCount: () => 0,
  isConversationNotFound: () => mocks.isConversationNotFound,
  noteLoadedArrivals: mocks.noteLoadedArrivals,
}));
vi.mock("../models/StreamingMessage", () => ({
  connectToStream: mocks.connectToStream,
  disconnectFromStream: () => undefined,
  loadSnapshotWithStream: (chatId: string) => mocks.loadSnapshotWithStream(chatId),
}));
vi.mock("../models/ComposerAttachments", () => ({ uploadFilesToComposer: () => undefined }));
vi.mock("./transcript-scroll-engine", () => ({
  createTranscriptScrollEngine: () => ({
    setChat: () => undefined,
    detach: () => undefined,
    noteMessageSent: () => undefined,
    afterRender: () => undefined,
    computeRenderPlan: () => ({ topPadPx: 0, startIndex: 0, endIndex: 0, bottomPadPx: 0 }),
    isViewportInSpacer: () => false,
  }),
}));
// The views beside the transcript are not what this file pins; each becomes an inert component
// (a factory is hoisted, so it cannot share one constant).
vi.mock("./TranscriptScrollbar", () => ({ TranscriptScrollbar: { view: () => null } }));
vi.mock("./MessageInput", () => ({ MessageInput: { view: () => null } }));
vi.mock("./ModelProviderMenu", () => ({ ModelProviderMenu: { view: () => null } }));
vi.mock("./AgentTerminalPanel", () => ({ AgentTerminalPanel: { view: () => null } }));
vi.mock("./ActivityIndicator", () => ({ ActivityIndicator: { view: () => null } }));
vi.mock("./TerminalViewToggle", () => ({ TerminalViewToggle: { view: () => null } }));
vi.mock("./EmptySlot", () => ({ EmptySlot: { view: () => null } }));
vi.mock("./QueuedMessageView", () => ({ renderQueuedMessages: () => [] }));
vi.mock("./OutgoingMessageView", () => ({ renderOutgoingMessages: () => mocks.outgoingBubbles }));
vi.mock("./fast-mode-limit", () => ({ maybeApplyFastModeLimit: () => undefined }));
vi.mock("./FastModeNotice", () => ({ FastModeNotice: { view: () => null } }));

import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import { ChatPanel } from "./ChatPanel";
import { MESSAGE_LIST_CLASS } from "./conversation-rows";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown };

const AGENT_ID = "agent-1";

/** Every vnode in the tree, depth-first, component vnodes included (their attrs are what the
 *  assertions read; their bodies are not rendered). */
function flatten(node: unknown): AnyVnode[] {
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(flatten);
  const vnode = node as AnyVnode;
  return [vnode, ...flatten(vnode.children)];
}

function renderedText(node: unknown): string {
  return flatten(node)
    .map((vnode) => (typeof vnode.children === "string" ? vnode.children : ""))
    .join(" ");
}

function findByClass(node: unknown, className: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => {
    const attrs = vnode.attrs ?? {};
    return [attrs.class, attrs.className].some((v) => typeof v === "string" && v.includes(className));
  });
}

/** A Button by the `extra` class the page marks it with. */
function findButton(node: unknown, extra: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.attrs?.extra === extra);
}

function click(button: AnyVnode | undefined): void {
  expect(button, "the button should be rendered").toBeTruthy();
  (button!.attrs!.onclick as () => void)();
}

async function flushAsync(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

function mountPanel(): () => unknown {
  const panel = ChatPanel();
  panel.oninit!({ attrs: { chatId: AGENT_ID } } as never);
  return () => panel.view({ attrs: { chatId: AGENT_ID } } as m.Vnode<{ chatId: string }>);
}

/** A seeded chat (the Mind app's onboarding conversation) in `phase`, its create having failed
 *  with `error` in the failed phase. */
function seeded(phase: "awaiting_first_send" | "creating" | "failed", error: string | null = null): void {
  mocks.proto = {
    chat_id: AGENT_ID,
    name: "Getting started",
    account_id: phase === "awaiting_first_send" ? "" : "acct-1",
    phase,
    error,
    is_seeded: true,
  };
}

/** A chat whose create failed on `accountId`, with `error` as the reason. */
function failed(accountId: string, error: string): void {
  mocks.proto = {
    chat_id: AGENT_ID,
    name: "Chat 1",
    account_id: accountId,
    phase: "failed",
    error,
    is_seeded: false,
  };
}

/** A chat whose create is running. */
function creating(): void {
  mocks.proto = {
    chat_id: AGENT_ID,
    name: "Chat 1",
    account_id: "acct-1",
    phase: "creating",
    error: null,
    is_seeded: false,
  };
}

/** A chat with no seed that waits for its first send (an intake that could not launch it at once). */
function awaiting(accountId: string): void {
  mocks.proto = {
    chat_id: AGENT_ID,
    name: "Chat 2",
    account_id: accountId,
    phase: "awaiting_first_send",
    error: null,
    is_seeded: false,
  };
}

/** The list the page's bubbles sit in: the child of the scroll area's content, by its class. */
function bubbleListOf(tree: unknown): AnyVnode | undefined {
  const wrapper = findByClass(tree, "message-list-wrapper");
  return flatten(wrapper?.children).find((vnode) =>
    [vnode.attrs?.class, vnode.attrs?.className].includes(MESSAGE_LIST_CLASS),
  );
}

describe("ChatPanel over a provisional chat", () => {
  beforeEach(() => {
    mocks.isConversationNotFound = false;
    mocks.isChatListReceived = true;
    mocks.launchChat.mockReset();
    mocks.launchChat.mockImplementation(async () => ({}));
    mocks.chat = undefined;
    mocks.outgoingBubbles = [];
    mocks.fetchEvents.mockClear();
  });

  it("shows a chat being created as an empty conversation, with no placeholder text", () => {
    creating();
    const tree = mountPanel()();

    expect(renderedText(tree).trim()).toBe("");
    expect(findByClass(tree, "message-list-creating")).toBeTruthy();
    expect(bubbleListOf(tree)?.children).toEqual([]);
  });

  it("draws a created chat with no events as the same empty conversation, with no placeholder text", () => {
    creating();
    const render = mountPanel();
    const starting = render();
    mocks.proto = undefined;
    mocks.chat = chatSnapshotFixture(AGENT_ID);

    const started = render();

    expect(renderedText(started).trim()).toBe("");
    expect(findByClass(started, "message-list-empty")).toBeTruthy();
    expect(bubbleListOf(started)?.attrs).toEqual(bubbleListOf(starting)?.attrs);
  });

  it("keeps a message sent while the chat starts where the empty transcript after it puts the message", () => {
    const bubble = { tag: "div", key: "outgoing-0", attrs: { class: "outgoing-message" }, children: [] };
    mocks.outgoingBubbles = [bubble];
    creating();
    const render = mountPanel();

    const starting = render();
    mocks.proto = undefined;
    mocks.chat = chatSnapshotFixture(AGENT_ID);
    const started = render();

    expect(bubbleListOf(starting)?.children).toEqual([bubble]);
    expect(bubbleListOf(started)?.children).toEqual([bubble]);
  });

  it("shows an unseeded chat awaiting its first send as an empty conversation with no placeholder, not a failure", () => {
    awaiting("");
    const render = mountPanel();

    const tree = render();

    expect(findByClass(tree, "message-list-awaiting")).toBeTruthy();
    expect(findByClass(tree, "message-list-create-failed")).toBeUndefined();
    expect(findByClass(tree, "message-list-creating")).toBeUndefined();
    expect(renderedText(tree).trim()).toBe("");
    // Nothing to read: the chat has no seed and no agent.
    expect(mocks.fetchEvents).not.toHaveBeenCalled();
  });

  it("shows a failed create's reason and retries it on the record's account", () => {
    failed("acct-1", "mngr create exited with code 1");
    const render = mountPanel();

    const tree = render();

    expect(findByClass(tree, "message-list-create-failed")).toBeTruthy();
    expect(renderedText(tree)).toContain("mngr create exited with code 1");
    click(findButton(tree, "message-list-create-retry"));
    expect(mocks.launchChat).toHaveBeenCalledWith(AGENT_ID, "acct-1");
  });

  it("shows a refused relaunch's reason beside the create's, and forgets it once the chat is being created", async () => {
    failed("acct-1", "mngr create exited with code 1");
    mocks.launchChat.mockImplementationOnce(async () => {
      throw new Error("account acct-1 is on a lane this build does not have");
    });
    const render = mountPanel();
    click(findButton(render(), "message-list-create-retry"));
    await flushAsync();

    const refused = render();

    expect(renderedText(refused)).toContain("mngr create exited with code 1");
    expect(renderedText(refused)).toContain("account acct-1 is on a lane this build does not have");

    mocks.proto = {
      chat_id: AGENT_ID,
      name: "Chat 1",
      account_id: "acct-1",
      phase: "creating",
      error: null,
      is_seeded: false,
    };
    render();
    failed("acct-1", "mngr create exited with code 2");
    const tree = render();

    expect(renderedText(tree)).toContain("mngr create exited with code 2");
    expect(renderedText(tree)).not.toContain("is on a lane this build does not have");
  });
});

describe("ChatPanel over a transcript that 404'd", () => {
  beforeEach(() => {
    mocks.proto = undefined;
    mocks.chat = undefined;
    mocks.outgoingBubbles = [];
    mocks.isConversationNotFound = true;
    mocks.isChatListReceived = true;
    mocks.loadSnapshotWithStream.mockReset();
    mocks.loadSnapshotWithStream.mockImplementation(async () => undefined);
  });

  afterEach(() => {
    mocks.loadSnapshotWithStream.mockImplementation(async () => undefined);
  });

  it("draws a chat the page has not been told about yet as an empty chat, not as one with no conversation", () => {
    // A new chat's page can load, and its transcript 404, before the chat app's list reaches it.
    mocks.isChatListReceived = false;

    const tree = mountPanel()();

    expect(renderedText(tree)).not.toContain("No conversation data");
    expect(findByClass(tree, "message-list-loading")).toBeTruthy();
  });

  it("says a chat the app does not list has no conversation", () => {
    const tree = mountPanel()();

    expect(renderedText(tree)).toContain("No conversation data");
  });

  it("keeps a chat that has just come up empty while its transcript is reloaded", async () => {
    let finishReload: () => void = () => {};
    mocks.loadSnapshotWithStream.mockImplementation(
      () =>
        new Promise<undefined>((resolve) => {
          finishReload = () => resolve(undefined);
        }),
    );
    const render = mountPanel();
    render();
    mocks.chat = chatSnapshotFixture(AGENT_ID);
    mocks.chatsUpdatedListener?.();

    const reloading = render();

    expect(renderedText(reloading)).not.toContain("No conversation data");
    mocks.isConversationNotFound = false;
    finishReload();
    await flushAsync();
    expect(renderedText(render())).not.toContain("No conversation data");
  });
});

describe("ChatPanel over a seeded chat", () => {
  beforeEach(() => {
    mocks.isConversationNotFound = false;
    mocks.isChatListReceived = true;
    mocks.fetchEvents.mockClear();
    mocks.loadSnapshotWithStream.mockClear();
    mocks.connectToStream.mockClear();
    mocks.noteLoadedArrivals.mockClear();
    mocks.chat = undefined;
    mocks.chatsUpdatedListener = null;
  });

  it("shows the seed as a transcript, read once with no stream until its first agent lands", async () => {
    seeded("awaiting_first_send");
    const render = mountPanel();

    const tree = render();
    render();

    // The transcript path, not a provisional screen: the seed segment is what the page reads.
    expect(findByClass(tree, "message-list-creating")).toBeUndefined();
    expect(findByClass(tree, "message-list-empty")).toBeTruthy();
    expect(mocks.fetchEvents).toHaveBeenCalledTimes(1);
    expect(mocks.fetchEvents).toHaveBeenCalledWith(AGENT_ID);
    // No agent, so nothing for a stream to follow: connecting one would 404 and loop on reconnects.
    expect(mocks.loadSnapshotWithStream).not.toHaveBeenCalled();
    expect(mocks.connectToStream).not.toHaveBeenCalled();

    // The first send's create in flight keeps the transcript up too.
    seeded("creating");
    expect(findByClass(render(), "message-list-creating")).toBeUndefined();

    // The agent landed: the chat is listed, and the transcript is reloaded with its stream.
    mocks.proto = null;
    mocks.chat = chatSnapshotFixture(AGENT_ID);
    expect(mocks.chatsUpdatedListener).toBeTruthy();
    mocks.chatsUpdatedListener!();
    await flushAsync();

    expect(mocks.loadSnapshotWithStream).toHaveBeenCalledTimes(1);
    expect(mocks.loadSnapshotWithStream).toHaveBeenCalledWith(AGENT_ID);
    // The first send landed before the stream existed: the placed snapshot stands its bubble down.
    expect(mocks.noteLoadedArrivals).toHaveBeenCalledWith(AGENT_ID);
    expect(mocks.loadSnapshotWithStream.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.noteLoadedArrivals.mock.invocationCallOrder[0],
    );
  });

  it("shows a failed create's reason over a seeded chat like any other", () => {
    seeded("failed", "mngr create exited with code 3");
    const render = mountPanel();

    const tree = render();

    expect(findByClass(tree, "message-list-create-failed")).toBeTruthy();
    expect(renderedText(tree)).toContain("mngr create exited with code 3");
    expect(mocks.fetchEvents).not.toHaveBeenCalled();
  });
});
