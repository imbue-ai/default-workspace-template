// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import type m from "mithril";

// vi.mock factories are hoisted above module scope, so anything they close over must come from
// vi.hoisted. Mithril captures requestAnimationFrame at import time, and jsdom has none.
const mocks = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  return {
    proto: null as unknown,
    accountsLoaded: true,
    selectedAccount: null as { id: string } | null,
    launchChat: vi.fn(async (_agentId: string, _accountId: string) => ({})),
    openProviderChooser: vi.fn(),
    closeProviderChooser: vi.fn(),
  };
});

vi.mock("../models/AgentManager", () => ({
  getAgentById: () => undefined,
  getProtoAgent: () => mocks.proto,
  launchChat: (agentId: string, accountId: string) => mocks.launchChat(agentId, accountId),
  addAgentsUpdatedListener: () => undefined,
  removeAgentsUpdatedListener: () => undefined,
  buildAgentTerminalUrl: () => "",
  getTerminalUrl: () => "",
}));
vi.mock("../models/Providers", () => ({
  areAccountsLoaded: () => mocks.accountsLoaded,
  getSelectedAccount: () => mocks.selectedAccount,
  openProviderChooser: mocks.openProviderChooser,
  closeProviderChooser: mocks.closeProviderChooser,
}));
vi.mock("../models/Response", () => ({
  addMessageSentListener: () => undefined,
  removeMessageSentListener: () => undefined,
  evictEvents: () => undefined,
  fetchBackfillEvents: async () => undefined,
  fetchEvents: async () => undefined,
  fetchForwardEvents: async () => undefined,
  fetchWindowAtOffset: async () => undefined,
  getConversationLoadState: () => null,
  getEventsForAgent: () => [],
  getEventCount: () => 0,
  getFirstOffset: () => 0,
  getRenderVersion: () => 0,
  getTotalEventCount: () => 0,
  isConversationNotFound: () => false,
}));
vi.mock("../models/StreamingMessage", () => ({
  connectToStream: () => undefined,
  disconnectFromStream: () => undefined,
  loadSnapshotWithStream: async () => undefined,
}));
vi.mock("../models/ComposerAttachments", () => ({ uploadFilesToComposer: () => undefined }));
vi.mock("./transcript-scroll-engine", () => ({
  createTranscriptScrollEngine: () => ({
    setAgent: () => undefined,
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
vi.mock("./ModelBar", () => ({ ModelBar: { view: () => null } }));
vi.mock("./AgentTerminalPanel", () => ({ AgentTerminalPanel: { view: () => null } }));
vi.mock("./ActivityIndicator", () => ({ ActivityIndicator: { view: () => null } }));
vi.mock("./TerminalViewToggle", () => ({ TerminalViewToggle: { view: () => null } }));
vi.mock("./EmptySlot", () => ({ EmptySlot: { view: () => null } }));
vi.mock("./QueuedMessageView", () => ({ renderQueuedMessages: () => [] }));
vi.mock("./OutgoingMessageView", () => ({ renderOutgoingMessages: () => [] }));
vi.mock("./fast-mode-prompt", () => ({ maybePromptForFastMode: () => undefined }));

import { ChatPanel } from "./ChatPanel";

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
  panel.oninit!({ attrs: { agentId: AGENT_ID } } as never);
  return () => panel.view({ attrs: { agentId: AGENT_ID } } as m.Vnode<{ agentId: string }>);
}

function awaiting(): void {
  mocks.proto = { agent_id: AGENT_ID, name: "Chat 1", account_id: "", phase: "awaiting_account", error: null };
}

describe("ChatPanel over a provisional chat", () => {
  beforeEach(() => {
    mocks.launchChat.mockReset();
    mocks.launchChat.mockImplementation(async () => ({}));
    mocks.openProviderChooser.mockReset();
    mocks.closeProviderChooser.mockReset();
    mocks.accountsLoaded = true;
    mocks.selectedAccount = null;
    awaiting();
  });

  it("decides nothing before the account list has loaded", () => {
    mocks.accountsLoaded = false;
    const render = mountPanel();

    const tree = render();

    expect(renderedText(tree)).toContain("Checking which providers are signed in");
    expect(mocks.launchChat).not.toHaveBeenCalled();
    expect(mocks.openProviderChooser).not.toHaveBeenCalled();
  });

  it("offers the chooser once with nothing signed in, and keeps a button to reopen it", () => {
    const render = mountPanel();

    const tree = render();
    render();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    expect(renderedText(tree)).toContain("Sign in to a provider to start this chat");
    expect(mocks.launchChat).not.toHaveBeenCalled();
    // The chooser's sign-in launches this chat.
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onSignedIn: (accountId: string) => void };
    intent.onSignedIn("acct-2");
    expect(mocks.launchChat).toHaveBeenCalledWith(AGENT_ID, "acct-2");
    expect(findByClass(render(), "message-list-creating")).toBeTruthy();
  });

  it("launches at once on the selected account, closing the chooser, and only once", () => {
    mocks.selectedAccount = { id: "acct-1" };
    const render = mountPanel();

    const tree = render();
    render();

    expect(mocks.launchChat).toHaveBeenCalledTimes(1);
    expect(mocks.launchChat).toHaveBeenCalledWith(AGENT_ID, "acct-1");
    expect(mocks.closeProviderChooser).toHaveBeenCalled();
    expect(mocks.openProviderChooser).not.toHaveBeenCalled();
    expect(findByClass(tree, "message-list-creating")).toBeTruthy();
  });

  it("shows a refused launch's reason with a retry on the same account", async () => {
    mocks.selectedAccount = { id: "acct-1" };
    mocks.launchChat.mockImplementationOnce(async () => {
      throw new Error("account acct-1 is on a lane this build does not have");
    });
    const render = mountPanel();
    render();
    await flushAsync();

    const tree = render();

    expect(renderedText(tree)).toContain("account acct-1 is on a lane this build does not have");
    expect(findByClass(tree, "message-list-creating")).toBeUndefined();
    click(findButton(tree, "message-list-launch-retry"));
    expect(mocks.launchChat).toHaveBeenCalledTimes(2);
    expect(mocks.launchChat).toHaveBeenLastCalledWith(AGENT_ID, "acct-1");
    expect(findByClass(render(), "message-list-creating")).toBeTruthy();
  });

  it("forgets a refusal once the chat is being created, so a later failure shows its own reason", async () => {
    // Two pages of one waiting chat both launch on the selected account; the backend
    // takes one and refuses the other, and the push then moves both to creating.
    mocks.selectedAccount = { id: "acct-1" };
    mocks.launchChat.mockImplementationOnce(async () => {
      throw new Error("Chat agent-1 is not waiting to be launched");
    });
    const render = mountPanel();
    render();
    await flushAsync();
    expect(renderedText(render())).toContain("is not waiting to be launched");

    mocks.proto = { agent_id: AGENT_ID, name: "Chat 1", account_id: "acct-1", phase: "creating", error: null };
    render();
    mocks.proto = {
      agent_id: AGENT_ID,
      name: "Chat 1",
      account_id: "acct-1",
      phase: "failed",
      error: "mngr create exited with code 1",
    };
    const tree = render();

    expect(renderedText(tree)).toContain("mngr create exited with code 1");
    expect(renderedText(tree)).not.toContain("is not waiting to be launched");
  });

  it("shows a failed create's reason and retries it on the record's account", () => {
    mocks.proto = {
      agent_id: AGENT_ID,
      name: "Chat 1",
      account_id: "acct-1",
      phase: "failed",
      error: "mngr create exited with code 1",
    };
    const render = mountPanel();

    const tree = render();

    expect(findByClass(tree, "message-list-create-failed")).toBeTruthy();
    expect(renderedText(tree)).toContain("mngr create exited with code 1");
    click(findButton(tree, "message-list-create-retry"));
    expect(mocks.launchChat).toHaveBeenCalledWith(AGENT_ID, "acct-1");
  });
});
