import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";
import m from "mithril";
import type { QueryClient } from "@tanstack/query-core";
import { installLocalStoragePolyfill } from "@imbue/workspace-ui/src/testing/localStorage";

// The composer persists its draft in localStorage, which the node test env lacks.
installLocalStoragePolyfill();

// vi.mock factories are hoisted above module scope, so anything they close over must come from
// vi.hoisted. Mithril also captures requestAnimationFrame at import time, so it is polyfilled
// here too.
const mocks = vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
  // The node test env has no document; provide a minimal one for code that wires
  // document listeners when lifecycle hooks actually run (they don't in these
  // vnode-only tests, but imports must not explode).
  const listeners = new Map<string, ((event: unknown) => void)[]>();
  globalThis.document ??= {
    // Nothing in it to find: the composer reads the page's base path off a meta tag when dictation starts.
    querySelector: () => null,
    addEventListener: (type: string, fn: (event: unknown) => void) => {
      listeners.set(type, [...(listeners.get(type) ?? []), fn]);
    },
    removeEventListener: (type: string, fn: (event: unknown) => void) => {
      listeners.set(
        type,
        (listeners.get(type) ?? []).filter((f) => f !== fn),
      );
    },
  } as unknown as Document;
  // The composer gates its Claude-only interceptions on the agent's harness and its
  // working/idle branch (stop button, placeholder) on the agent's activity state --
  // both driven through this mutable holder (reset in beforeEach; an undefined
  // activity_state means "not working").
  const agent: {
    harness: string | undefined;
    activity_state: string | undefined;
    account_id: string | null;
    is_connecting: boolean;
  } = {
    harness: "claude",
    activity_state: undefined,
    account_id: null,
    is_connecting: false,
  };
  // The chat's in-progress switch, the account its next send switches it to, and the model
  // picked for it, all null at rest.
  const switching: {
    handoff: unknown;
    target: { id: string; harness: string; label: string } | null;
    pick: { identity: { model_id: string; effort: string | null; fast: boolean }; label: string } | null;
    kind: "handoff" | "rebind";
  } = {
    handoff: null,
    target: null,
    kind: "handoff",
    pick: null,
  };
  return {
    sendMessage: vi.fn(async (): Promise<string | null> => "ok"),
    switchChat: vi.fn(async () => ({ kind: "handoff", phase: "summarizing", returned_block: "" })),
    setPendingAccount: vi.fn(),
    openSwitchDialog: vi.fn(),
    beginSwitchToAccountId: vi.fn(),
    launchAwaitingChatAsRoom: vi.fn(async (_chatId: string) => {}),
    // What the composer registered to hear of a model pick the chat app refused.
    modelChoiceFailureListener: null as ((chatId: string, detail: string) => void) | null,
    // The account ids that read as signed out; empty at rest.
    signedOutAccountIds: new Set<string>(),
    loadAccounts: vi.fn(async () => {}),
    cancelHandoff: vi.fn(async () => ({ returned_block: "" })),
    switching,
    drainToComposer: vi.fn(async () => ({ block: "" })),
    getComposerAttachments: vi.fn(() => [] as unknown[]),
    clearComposerAttachments: vi.fn(),
    uploadDescribedFileToComposer: vi.fn(),
    // Resolved at once by default: no upload in flight. A test of a send waiting on one swaps in a
    // deferred promise.
    waitForComposerUploads: vi.fn(async (_chatId: string) => {}),
    interruptAgent: vi.fn(async () => {}),
    // Whether the agent's turn for a message has reached the transcript; none has, at rest.
    hasUserTurnLandedSince: vi.fn((_chatId: string, _text: string, _priorTurnIds: ReadonlySet<string>) => false),
    openProviderChooser: vi.fn(),
    // Resolved at once by default: the agent exists. A test of a chat still being created
    // swaps in a deferred promise.
    whenChatRegistered: vi.fn(async (_chatId: string) => {}),
    refuseSendToFailedChat: vi.fn((_chatId: string) => {}),
    // Whether the chat list names the chat; false for a seeded chat awaiting its first send.
    isChatRegistered: true,
    isLaunchInFlight: false,
    // Whether the chat is a room (a message log among the people here), which offers no model and no agent.
    isRoom: false,
    provisional: undefined as unknown,
    launchChat: vi.fn(async (_chatId: string, _accountId: string, _message?: string, _kind?: string) => ({})),
    chooseFastMode: vi.fn(),
    selectedAccount: null as { id: string } | null,
    // Whether this page has read the account list; until it has, no account reads as missing.
    accountsLoaded: true,
    listeners,
    agent,
    // Whether the page offers dictation, and the dictation the composer made: a stand-in whose state the test
    // sets and whose events the test fires, as the engine would.
    isDictationSupported: false,
    // The kind of device the composer asked dictation for, once per dictation it made.
    dictationDeviceClasses: [] as string[],
    // Every value the composer handed the screen wake lock, in order.
    screenWakeWanted: [] as boolean[],
    makeFakeDictation: () => ({
      state: "idle",
      isHeld: false,
      start: vi.fn(async () => {}),
      stop: vi.fn(async () => {}),
      cancel: vi.fn(async () => {}),
      retryHeld: vi.fn(),
      discardHeld: vi.fn(),
      dispose: vi.fn(),
    }),
    dictation: null as null | {
      state: string;
      isHeld: boolean;
      start: ReturnType<typeof vi.fn>;
      stop: ReturnType<typeof vi.fn>;
      cancel: ReturnType<typeof vi.fn>;
      retryHeld: ReturnType<typeof vi.fn>;
      discardHeld: ReturnType<typeof vi.fn>;
      dispose: ReturnType<typeof vi.fn>;
    },
    dictationEvents: null as null | {
      onStateChange(state: string): void;
      onLevel(level: number): void;
      onFinalWords(words: string): void;
      onTentativeWords(words: string): void;
      onUtteranceEnd(): void;
      onProblem(problem: unknown): void;
    },
    // What the probe at mount says of the speech model: on this device unless a test says otherwise.
    speechModelProbe: vi.fn(async () => ({ isDownloaded: true, totalBytes: 250_000_000, device: "wasm" })),
    readyChime: { wake: vi.fn(), play: vi.fn() },
    // The page's speech models: a real query client and a stand-in for the models over it, both made afresh
    // for each dictation test.
    voiceQueryClient: null as unknown as QueryClient,
    speechModels: null as unknown as FakeSpeechModels,
  };
});

vi.mock("../models/Response", () => ({
  getEventsForChat: () => [],
  hasUserTurnLandedSince: mocks.hasUserTurnLandedSince,
  sentUserTurnIds: () => new Set(["turn-before-the-send"]),
  sendMessage: mocks.sendMessage,
  drainToComposer: mocks.drainToComposer,
  interruptAgent: mocks.interruptAgent,
  mintMessageId: () => `m-${Math.random().toString(36).slice(2)}`,
}));
vi.mock("../models/Handoffs", () => ({ switchChat: mocks.switchChat, cancelHandoff: mocks.cancelHandoff }));
// The armed choice is served from here; whether a switch request is out is the real module's.
vi.mock("../models/PendingLane", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../models/PendingLane")>();
  return {
    isSwitchSending: actual.isSwitchSending,
    setSwitchSending: actual.setSwitchSending,
    nextSendSwitchTarget: (chatId: string) =>
      mocks.switching.handoff !== null || actual.isSwitchSending(chatId) ? null : mocks.switching.target,
    getPendingPick: () => mocks.switching.pick,
    setPendingAccount: mocks.setPendingAccount,
    switchKind: () => mocks.switching.kind,
  };
});
vi.mock("./SwitchDialog", () => ({
  openSwitchDialog: mocks.openSwitchDialog,
  beginSwitchToAccountId: mocks.beginSwitchToAccountId,
  launchAwaitingChatAsRoom: mocks.launchAwaitingChatAsRoom,
}));
vi.mock("./fast-mode-limit", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./fast-mode-limit")>()),
  chooseFastMode: mocks.chooseFastMode,
}));
vi.mock("../models/ComposerAttachments", () => ({
  clearComposerAttachments: mocks.clearComposerAttachments,
  getComposerAttachments: mocks.getComposerAttachments,
  getReadyAttachmentPaths: () =>
    (mocks.getComposerAttachments() as { status: string; uploaded?: { path: string } }[]).flatMap((attachment) =>
      attachment.status === "ready" && attachment.uploaded ? [attachment.uploaded.path] : [],
    ),
  hasReadyAttachments: () => false,
  removeComposerAttachment: vi.fn(),
  restoreComposerAttachments: vi.fn(),
  uploadDescribedFileToComposer: mocks.uploadDescribedFileToComposer,
  uploadFilesToComposer: vi.fn(),
  waitForComposerUploads: (chatId: string) => mocks.waitForComposerUploads(chatId),
}));
vi.mock("../models/attachments", () => ({
  buildMessageWithAttachments: (text: string, paths: readonly string[]) =>
    paths.length === 0 ? text : `${text} + ${paths.join(" ")}`,
  formatFileSize: () => "0 B",
}));
vi.mock("@imbue/workspace-ui/src/models/request-error", () => ({
  describeRequestError: (e: unknown) => String(e),
  describeRequestErrorKind: (e: unknown) =>
    typeof e === "object" && e !== null && "kind" in e ? (e as { kind: string }).kind : "unknown",
  isUnansweredRequest: (e: unknown) => typeof e === "object" && e !== null && (e as { code?: unknown }).code === 0,
}));
vi.mock("../models/ModelSettings", () => ({
  addModelChoiceFailureListener: (listener: (chatId: string, detail: string) => void) => {
    mocks.modelChoiceFailureListener = listener;
  },
  effectiveChoice: () => null,
  isPickInFlight: () => false,
  setModelChoice: vi.fn(),
}));
// The composer guard follows whatever popups the harness declared on its catalog,
// so the mock ships a per-harness fixture mirroring the real declarations (the
// matcher itself is reimplemented here minimally; the real one is covered by
// HarnessCatalog.test.ts).
vi.mock("../models/HarnessCatalog", async () => {
  const { harnessCatalogFixture } = await import("../models/harnessCatalogFixture");
  const catalogs: Record<
    string,
    {
      label: string;
      can_interrupt_compaction: boolean;
      popups: { trigger: string; commands: string[]; action: string }[];
    }
  > = {
    claude: {
      label: harnessCatalogFixture("claude")!.label,
      can_interrupt_compaction: harnessCatalogFixture("claude")!.can_interrupt_compaction,
      popups: [
        { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
        { trigger: "composer_command", commands: ["/status", "/exit"], action: "notice" },
      ],
    },
    codex: {
      label: harnessCatalogFixture("codex")!.label,
      can_interrupt_compaction: harnessCatalogFixture("codex")!.can_interrupt_compaction,
      popups: [
        { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
        { trigger: "composer_command", commands: ["/new", "/fast"], action: "notice" },
      ],
    },
  };
  const getHarnessCatalog = (harness?: string) => (harness ? (catalogs[harness] ?? null) : null);
  return {
    ensureHarnessCatalogs: vi.fn(async () => {}),
    getHarnessCatalog,
    // Both fixture harnesses have fast mode, so ``/fast on`` and ``/fast off`` pick the chat's mode.
    hasFastModeLimit: (harness?: string) => harness === "claude" || harness === "codex",
    findComposerPopup: (harness: string | undefined, text: string) => {
      const firstToken = text.trim().toLowerCase().split(/\s+/, 1)[0] ?? "";
      for (const popup of getHarnessCatalog(harness)?.popups ?? []) {
        if (popup.trigger === "composer_command" && popup.commands.includes(firstToken)) {
          return { popup, command: firstToken };
        }
      }
      return null;
    },
  };
});
vi.mock("../models/Chats", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../models/Chats")>()),
  getChatById: () =>
    mocks.isChatRegistered ? { active_agent: mocks.agent, handoff: mocks.switching.handoff } : undefined,
  getProvisionalChat: () => mocks.provisional,
  isLaunchInFlight: () => mocks.isLaunchInFlight,
  isRoomChat: () => mocks.isRoom,
  launchChat: (chatId: string, accountId: string, message?: string, kind?: string) =>
    mocks.launchChat(chatId, accountId, message, kind),
  whenChatRegistered: (chatId: string) => mocks.whenChatRegistered(chatId),
  refuseSendToFailedChat: (chatId: string) => mocks.refuseSendToFailedChat(chatId),
}));
vi.mock("../models/Providers", () => ({
  openProviderChooser: mocks.openProviderChooser,
  // The seeded chats these tests launch name no account of their own, so the selected one decides.
  accountForFirstSend: () => mocks.selectedAccount,
  areAccountsLoaded: () => mocks.accountsLoaded,
  isAccountSignedOut: (accountId: string | null | undefined) =>
    !!accountId && mocks.signedOutAccountIds.has(accountId),
  loadAccounts: () => mocks.loadAccounts(),
  whenAccountsReadyToChoose: () => Promise.resolve(),
}));
vi.mock("@imbue/workspace-voice/src/dictation", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@imbue/workspace-voice/src/dictation")>();
  return {
    describeSpeechModelLoadError: actual.describeSpeechModelLoadError,
    isSpeechModelLoadProblem: actual.isSpeechModelLoadProblem,
    createDictation: (_options: unknown, events: NonNullable<typeof mocks.dictationEvents>) => {
      mocks.dictation = mocks.makeFakeDictation();
      mocks.dictationEvents = events;
      return mocks.dictation;
    },
  };
});
vi.mock("../models/screenWakeLock", () => ({
  browserWakeLockHost: () => null,
  createScreenWakeLock: () => ({ setWanted: (isWanted: boolean) => mocks.screenWakeWanted.push(isWanted) }),
}));
vi.mock("@imbue/workspace-voice/src/speechModel", () => ({
  getVoiceQueryClient: () => mocks.voiceQueryClient,
  getSpeechModels: () => mocks.speechModels,
}));
vi.mock("@imbue/workspace-voice/src/support", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@imbue/workspace-voice/src/support")>()),
  isDictationSupported: () => mocks.isDictationSupported,
  pageDictationOptions: (_storage: unknown, _basePath: string, _pageUrl: string, deviceClass: string) => {
    mocks.dictationDeviceClasses.push(deviceClass);
    return { engineId: "moonshine-base", preferredDevice: null, assetBase: "https://chat.test/assets/voice/" };
  },
}));
vi.mock("@imbue/workspace-voice/src/downloadProbe", () => ({
  probeEngineDownload: () => mocks.speechModelProbe(),
}));
vi.mock("@imbue/workspace-voice/src/readyChime", () => ({ createReadyChime: () => mocks.readyChime }));

import { TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { jsonBlock } from "@imbue/workspace-ui/src/element_reference";
import { isApplePlatform } from "@imbue/workspace-voice/src/support";
import { EngineLoadError } from "@imbue/workspace-voice/src/workerClient";
import { handoffStateFixture } from "../models/chatSnapshotFixture";
import { DICTATION_NOTE, hasDictatedSpeech, withDictationNote } from "../models/dictationNote";
import { setMentionsForTesting } from "../models/Mentions";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import {
  MAX_TEXTAREA_HEIGHT_PX,
  MessageInput,
  prependToComposer,
  raiseFailureNotice,
  restoreComposerDraft,
  setComposerHint,
  stopDictationWhenHidden,
  takeComposerDraft,
} from "./MessageInput";

type AnyVnode = { tag?: unknown; attrs?: Record<string, unknown>; children?: unknown; text?: unknown };

/** The page's speech models as a dictation test stands in for them: a request runs the load as a real query on
 *  the test's client, which the test settles through ``loads``; discovery answers when the test says. */
interface FakeSpeechModels {
  /** Every load a request started, in order, each for the test to settle. */
  readonly loads: { resolve(model: { device: "wasm" }): void; reject(error: Error): void }[];
  finishDiscovery(): void;
  loadQueryOptions(): { queryKey: readonly unknown[]; queryFn: () => Promise<{ device: "wasm" }> };
  progressQueryKey(): readonly unknown[];
  request: ReturnType<typeof vi.fn<() => Promise<{ device: "wasm" }>>>;
  discover: ReturnType<typeof vi.fn<(options: unknown, isDownloaded: boolean) => Promise<void>>>;
}

/** Every vnode in the tree, depth-first. */
function flatten(node: unknown): AnyVnode[] {
  if (node === null || node === undefined || typeof node !== "object") {
    return [];
  }
  if (Array.isArray(node)) {
    return node.flatMap(flatten);
  }
  const vnode = node as AnyVnode;
  // A closure-component vnode (e.g. m(Button, ...)) carries no markup of its
  // own -- its view runs only when mithril renders it -- so expand it and
  // flatten what it renders.
  if (typeof vnode.tag === "function") {
    const component = (vnode.tag as (v: AnyVnode) => { view: (v: AnyVnode) => unknown })(vnode);
    return flatten(component.view(vnode));
  }
  // Likewise for object component vnodes (an instance with a view method),
  // e.g. NoticeDialog: their markup does not exist in this tree until the
  // view is asked for it.
  const tag = vnode.tag as unknown;
  if (tag !== null && typeof tag === "object" && typeof (tag as { view?: unknown }).view === "function") {
    // Children come along too: NoticeDialog renders its body through the Modal
    // shell, which reads it off vnode.children.
    const rendered = (tag as { view: (v: unknown) => unknown }).view({
      attrs: vnode.attrs ?? {},
      children: vnode.children,
    });
    return [vnode, ...flatten(rendered)];
  }
  return [vnode, ...flatten(vnode.children)];
}

/** All literal text rendered in the tree, so a notice can be asserted on by its wording. */
function renderedText(node: unknown): string {
  return flatten(node)
    .map((vnode) =>
      typeof vnode.text === "string" ? vnode.text : typeof vnode.children === "string" ? vnode.children : "",
    )
    .join(" ");
}

function findByClass(node: unknown, className: string): AnyVnode | undefined {
  // Mithril's hyperscript normalizes a `class` attribute onto `className`, so check both.
  return flatten(node).find((vnode) => {
    const attrs = vnode.attrs ?? {};
    return [attrs.class, attrs.className].some((v) => typeof v === "string" && v.includes(className));
  });
}

/** The first onEscape guard in the tree -- the attr a dialog hands the Modal shell. Walks the
 *  raw vnodes rather than flatten(), which drops closure-component vnodes (Modal is one). */
function findEscapeGuard(node: unknown): (() => void) | undefined {
  if (node === null || node === undefined || typeof node !== "object") {
    return undefined;
  }
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findEscapeGuard(child);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  const vnode = node as AnyVnode;
  if (typeof vnode.attrs?.onEscape === "function") {
    return vnode.attrs.onEscape as () => void;
  }
  if (typeof vnode.tag === "function") {
    const component = (vnode.tag as (v: AnyVnode) => { view: (v: AnyVnode) => unknown })(vnode);
    return findEscapeGuard(component.view(vnode));
  }
  const tag = vnode.tag as unknown;
  if (tag !== null && typeof tag === "object" && typeof (tag as { view?: unknown }).view === "function") {
    const rendered = (tag as { view: (v: unknown) => unknown }).view({
      attrs: vnode.attrs ?? {},
      children: vnode.children,
    });
    return findEscapeGuard(rendered);
  }
  return findEscapeGuard(vnode.children);
}

/** Let queued promise callbacks run. The notice's buttons are `() => void action()`, which is
 *  right for mithril but discards the promise, so awaiting the handler does not await the work. */
async function flushAsync(): Promise<void> {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** Find a button by its visible label. The tooltip is not inspectable -- hoverTooltipAttrs
 *  returns lifecycle hooks rather than an attribute -- so the label is the stable handle. */
function findButton(node: unknown, label: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === "button" && renderedText(vnode).includes(label));
}

function findByTag(node: unknown, tag: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === tag);
}

/** Find a vnode by an exact attribute value (e.g. a stable aria-label). */
function findByAttr(node: unknown, attr: string, value: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => (vnode.attrs ?? {})[attr] === value);
}

/** Hold the next send's upload wait open; the returned function lets it finish. */
function holdComposerUploads(): () => void {
  let uploaded: () => void = () => undefined;
  mocks.waitForComposerUploads.mockReturnValueOnce(
    new Promise<void>((resolve) => {
      uploaded = resolve;
    }),
  );
  return () => uploaded();
}

/** Render the composer for one agent, type `text`, then press the send button. */
async function typeAndSend(component: m.Component<{ chatId: string | null }>, chatId: string, text: string) {
  const render = () => component.view!({ attrs: { chatId } } as never);
  const textarea = findByTag(render(), "textarea");
  const oninput = textarea?.attrs?.oninput as ((event: unknown) => void) | undefined;
  oninput?.({ target: { value: text, style: {}, scrollHeight: 10 } });

  const sendButton = findByAttr(render(), "aria-label", "Send message");
  const onclick = sendButton?.attrs?.onclick as (() => Promise<void>) | undefined;
  expect(onclick, "send button should be present once text is typed").toBeTruthy();
  await onclick!();
  return render();
}

describe("MessageInput send guard", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.openProviderChooser.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("puts the chat in the mode /fast on or /fast off names instead of sending it", async () => {
    mocks.agent.harness = "codex";
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/fast on");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(mocks.chooseFastMode).toHaveBeenCalledWith("agent-1", "on", []);
    expect(renderedText(after)).not.toContain("can't be sent from chat");
    // The command was acted on, so the composer is empty again.
    const textarea = findByTag(after, "textarea");
    expect(textarea?.attrs?.value).toBe("");
    // A bare /fast names no mode, so the harness's own notice still explains it.
    const declined = await typeAndSend(MessageInput(), "agent-1", "/fast");
    expect(renderedText(declined)).toContain("/fast can't be sent from chat");
    expect(mocks.chooseFastMode).toHaveBeenCalledTimes(1);
  });

  it("does not send /status, and explains why", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "/status");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("/status can't be sent from chat");
    expect(text).toContain("You can still send it from the agent's terminal.");
  });

  it("keeps the typed message so it is not lost", async () => {
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "/status");
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.value).toBe("/status");
  });

  it("still sends an ordinary message", async () => {
    await typeAndSend(MessageInput(), "agent-1", "hello there");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello there", expect.stringMatching(/^m-/));
  });

  it("drops NUL characters from a pasted message rather than sending them", async () => {
    await typeAndSend(MessageInput(), "agent-1", "before\0after\0");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "beforeafter", expect.stringMatching(/^m-/));
  });

  it("still declines a command that a NUL character splits", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "/sta\0tus");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("/status can't be sent from chat");
  });

  it("still sends a slash command that does not take over the input box", async () => {
    await typeAndSend(MessageInput(), "agent-1", "/clear");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "/clear", expect.stringMatching(/^m-/));
  });

  it("does not send /exit either, with the same notice", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "/exit");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("/exit can't be sent from chat");
    expect(text).toContain("You can still send it from the agent's terminal.");
  });

  it("dismisses the notice on Escape", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    // The notice hands its Escape guard to the Modal shell via onEscape; the
    // shell's real document listener is covered in NoticeDialog.test.ts (these
    // tests render vnodes with no DOM), so fire the guard directly here.
    const escapeGuard = findEscapeGuard(after);
    expect(escapeGuard, "notice should hand the shell an Escape guard").toBeTypeOf("function");
    escapeGuard!();

    const reRendered = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(reRendered)).not.toContain("can't be sent from chat");
  });

  it("sends a command another harness never declared", async () => {
    // /status is claude's declaration; codex declared its own list, and /status
    // is not on it, so for a codex agent it goes through with no notice.
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/status");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "/status", expect.stringMatching(/^m-/));
    expect(renderedText(after)).not.toContain("can't be sent from chat");
  });

  it("declines the commands the other harness declared", async () => {
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/new");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("/new can't be sent from chat");
  });

  it("intercepts an auth command with the agent-auth notice, even with arguments", async () => {
    const component = MessageInput();
    // The argument form must intercept too: matched on the first token, so
    // "/login please" cannot slip past the guard mid-fetch or mid-typo.
    const after = await typeAndSend(component, "agent-1", "/login please");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("Sign-in is managed here");

    // The notice offers the provider chooser, which signs in to an account of its
    // own rather than running the agent's auth flow inside the agent's terminal.
    const openButton = findByClass(after, "btn--primary");
    (openButton?.attrs?.onclick as (() => void) | undefined)?.();
    expect(mocks.openProviderChooser).toHaveBeenCalled();
  });

  it("intercepts auth commands for every harness that declared them", async () => {
    mocks.agent.harness = "codex";
    const after = await typeAndSend(MessageInput(), "agent-1", "/login");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(renderedText(after)).toContain("Sign-in is managed here");
  });

  it("does not carry the notice over to another agent", async () => {
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "/status");
    expect(renderedText(after)).toContain("can't be sent from chat");

    const switched = component.view!({ attrs: { chatId: "agent-2" } } as never);
    expect(renderedText(switched)).not.toContain("can't be sent from chat");
  });
});

describe("MessageInput placeholder", () => {
  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("shows the base wording while the agent is idle", () => {
    const textarea = findByTag(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("Type a message...");
  });

  it("teaches queueing while the agent has a turn in flight", () => {
    mocks.agent.activity_state = "THINKING";
    const textarea = findByTag(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("Type to queue more messages...");
  });

  it("teaches queueing while the agent compacts its context", () => {
    mocks.agent.activity_state = "COMPACTING";
    const textarea = findByTag(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("Type to queue more messages...");
  });

  it("tells a room's sender to tag the agent, until a switch off the room holds the message instead", () => {
    mocks.isRoom = true;
    try {
      const room = findByTag(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
      expect(room?.attrs?.placeholder).toBe("Message (tag @agent to ask the agent)");

      mocks.switching.handoff = handoffStateFixture({ phase: "summarizing" });
      const switching = findByTag(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
      expect(switching?.attrs?.placeholder).toBe("Type a message; it is delivered once Codex is ready…");
    } finally {
      mocks.isRoom = false;
      mocks.switching.handoff = null;
    }
  });
});

describe("MessageInput attachment chips", () => {
  const render = () => MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);

  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  afterEach(() => {
    mocks.getComposerAttachments.mockReturnValue([]);
  });

  function chip(tree: unknown, fileName: string): AnyVnode | undefined {
    return flatten(tree).find(
      (vnode) =>
        typeof vnode.attrs?.className === "string" &&
        vnode.attrs.className.includes("composer-attachment--") &&
        flatten(vnode).some((inner) => inner.attrs?.alt === fileName || renderedText(inner).includes(fileName)),
    );
  }

  it("puts the attachments inside the text's row, marked so it lays them out between the text and the buttons", () => {
    const withoutAttachments = findByClass(render(), "message-input-row");
    expect(withoutAttachments?.attrs?.["data-attachments"]).toBeUndefined();
    expect(findByClass(withoutAttachments, "composer-attachment")).toBeUndefined();

    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.txt", isImage: false, status: "uploading", localSize: 7 },
    ]);
    const row = findByClass(render(), "message-input-row");
    expect(row?.attrs?.["data-attachments"]).toBe("");
    expect(chip(row, "notes.txt")).toBeDefined();
  });

  it("shows an uploading image as its own pixels under a spinner, with no way to remove it yet", () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "photo.png", isImage: true, status: "uploading", previewUrl: "blob:local" },
    ]);
    const image = chip(render(), "photo.png");
    expect(findByTag(image, "img")?.attrs?.src).toBe("blob:local");
    expect(findByAttr(image, "aria-label", "Uploading")).toBeDefined();
    expect(findByAttr(image, "aria-label", "Remove attachment")).toBeUndefined();
  });

  it("shows an uploading file by name and local size, with a spinner where its icon goes", () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.txt", isImage: false, status: "uploading", localSize: 7 },
    ]);
    const file = chip(render(), "notes.txt");
    expect(findByTag(file, "img")).toBeUndefined();
    expect(findByAttr(findByClass(file, "composer-attachment-icon"), "aria-label", "Uploading")).toBeDefined();
    expect(findByClass(file, "composer-attachment-summary")).toBeDefined();
    expect(findByAttr(file, "aria-label", "Remove attachment")).toBeUndefined();
  });

  it("shows a ready image as its uploaded thumbnail alone, removable", () => {
    mocks.getComposerAttachments.mockReturnValue([
      {
        localId: "a1",
        fileName: "photo.png",
        isImage: true,
        status: "ready",
        uploaded: { path: "/uploads/photo.png", name: "photo.png", size: 6, isImage: true, url: "/files/photo.png" },
      },
    ]);
    const image = chip(render(), "photo.png");
    expect(findByTag(image, "img")?.attrs?.src).toBe("/files/photo.png");
    expect(findByClass(image, "composer-attachment-name")).toBeUndefined();
    expect(findByAttr(image, "aria-label", "Uploading")).toBeUndefined();
    expect(findByAttr(image, "aria-label", "Remove attachment")).toBeDefined();
  });

  it("says a failed upload failed, even for an image, and lets it be removed", () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "photo.png", isImage: true, status: "error", error: "boom" },
    ]);
    const failed = chip(render(), "photo.png");
    expect(findByTag(failed, "img")).toBeUndefined();
    expect(renderedText(failed)).toContain("Upload failed");
    expect(findByAttr(failed, "aria-label", "Remove attachment")).toBeDefined();
  });
});

describe("MessageInput send button", () => {
  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("stays in place, disabled, with nothing to send, and is enabled once there is text", () => {
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(render(), "aria-label", "Send message")?.attrs?.disabled).toBe(true);
    const oninput = findByTag(render(), "textarea")?.attrs?.oninput as (event: unknown) => void;
    oninput({ target: { value: "hello", style: {}, scrollHeight: 10 } });
    expect(findByAttr(render(), "aria-label", "Send message")?.attrs?.disabled).toBe(false);
  });
});

/** The composer's row as fitComposer touches it, holding its attributes in `attributes`. */
function fakeComposerRow(attributes: Set<string>) {
  return {
    clientWidth: 600,
    style: {},
    getBoundingClientRect: () => ({ height: 36 }),
    hasAttribute: (name: string) => attributes.has(name),
    removeAttribute: (name: string) => attributes.delete(name),
    toggleAttribute: (name: string, force: boolean) => {
      if (force) attributes.add(name);
      else attributes.delete(name);
      return force;
    },
  };
}

/** Stand in for ResizeObserver, which jsdom lacks and the composer row constructs when it mounts. */
function stubInertResizeObserver(): void {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe(): void {}
      disconnect(): void {}
    },
  );
}

describe("MessageInput layout with attachments", () => {
  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
    stubInertResizeObserver();
  });

  afterEach(() => {
    mocks.getComposerAttachments.mockReturnValue([]);
    vi.unstubAllGlobals();
  });

  /** Lay the composer out, as the browser would, for one line of text that fits; returns whether the row stacked. */
  function stacksOneLineOfText(): boolean {
    const tree = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    const rowVnode = findByClass(tree, "message-input-row");
    const attributes = new Set<string>();
    if (rowVnode?.attrs?.["data-attachments"] !== undefined) attributes.add("data-attachments");
    const row = fakeComposerRow(attributes);
    const textarea = { style: {}, scrollHeight: 28, clientHeight: 28, focus: () => undefined };
    (rowVnode?.attrs?.oncreate as (vnode: unknown) => void)({ dom: row });
    (findByTag(tree, "textarea")?.attrs?.onupdate as (vnode: unknown) => void)({ dom: textarea });
    return attributes.has("data-stacked");
  }

  it("keeps one line of text beside its buttons with no attachments", () => {
    expect(stacksOneLineOfText()).toBe(false);
  });

  it("stacks the row whenever it holds attachments, even for one line of text", () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.txt", isImage: false, status: "uploading", localSize: 7 },
    ]);
    expect(stacksOneLineOfText()).toBe(true);
  });
});

describe("MessageInput draft scroll position", () => {
  const ONE_LINE_PX = 28;
  const DRAFT_CONTENT_PX = 600;

  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    localStorage.clear();
    stubInertResizeObserver();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** A textarea holding a draft taller than the composer's cap, laid out the way the browser would: one line
   *  tall while its height is "auto", and its scrollTop clamped to the range its height leaves. The draft ends with
   *  `draftSuffix`, and the caret sits at `caretPosition`, or at the end. With `isScrollLostOnCollapse`, collapsing
   *  it to "auto" resets its scrollTop, as some browsers do. `scrollTopWriteCount` counts the writes to its
   *  scrollTop. */
  function makeOverflowingDraftTextarea({
    caretPosition,
    draftSuffix = "",
    isScrollLostOnCollapse = false,
  }: { caretPosition?: number; draftSuffix?: string; isScrollLostOnCollapse?: boolean } = {}) {
    let storedScrollTopPx = 0;
    let heightStyle = "";
    const value = "a long draft ".repeat(40) + draftSuffix;
    const caret = caretPosition ?? value.length;
    return {
      value,
      selectionStart: caret,
      selectionEnd: caret,
      scrollTopWriteCount: 0,
      style: {
        overflowY: "",
        get height(): string {
          return heightStyle;
        },
        set height(next: string) {
          heightStyle = next;
          if (next === "auto" && isScrollLostOnCollapse) storedScrollTopPx = 0;
        },
      },
      focus: () => undefined,
      setSelectionRange(start: number, end: number): void {
        this.selectionStart = start;
        this.selectionEnd = end;
      },
      get clientHeight(): number {
        return this.style.height === "" || this.style.height === "auto" ? ONE_LINE_PX : parseFloat(this.style.height);
      },
      get scrollHeight(): number {
        return Math.max(DRAFT_CONTENT_PX, this.clientHeight);
      },
      get scrollTop(): number {
        return Math.min(storedScrollTopPx, this.scrollHeight - this.clientHeight);
      },
      set scrollTop(valuePx: number) {
        this.scrollTopWriteCount += 1;
        storedScrollTopPx = Math.max(0, Math.min(valuePx, this.scrollHeight - this.clientHeight));
      },
    };
  }

  /** Mount the composer around `textarea` and lay it out once; returns the textarea's input, key and redraw
   *  hooks. */
  function mountComposer(textarea: ReturnType<typeof makeOverflowingDraftTextarea>) {
    const tree = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    const textareaVnode = findByTag(tree, "textarea");
    const onupdate = textareaVnode?.attrs?.onupdate as (vnode: unknown) => void;
    const oninput = textareaVnode?.attrs?.oninput as (event: unknown) => void;
    const onkeydown = textareaVnode?.attrs?.onkeydown as (event: unknown) => void;
    (findByClass(tree, "message-input-row")?.attrs?.oncreate as (vnode: unknown) => void)({
      dom: fakeComposerRow(new Set()),
    });
    onupdate({ dom: textarea });
    return {
      redraw: () => onupdate({ dom: textarea }),
      type: () => oninput({ target: textarea }),
      press: (key: string) => onkeydown({ key, preventDefault: () => undefined }),
    };
  }

  it("leaves an overflowing draft where its reader scrolled it when the composer redraws", () => {
    const textarea = makeOverflowingDraftTextarea();
    const composer = mountComposer(textarea);
    expect(textarea.clientHeight).toBe(MAX_TEXTAREA_HEIGHT_PX);
    textarea.scrollTop = 40;
    const writeCountBeforeRedraw = textarea.scrollTopWriteCount;

    composer.redraw();

    expect(textarea.scrollTop).toBe(40);
    expect(textarea.scrollTopWriteCount).toBe(writeCountBeforeRedraw);
  });

  it("keeps an overflowing draft's scroll position across a redraw where collapsing it to measure loses it", () => {
    const textarea = makeOverflowingDraftTextarea({ isScrollLostOnCollapse: true });
    const composer = mountComposer(textarea);
    textarea.scrollTop = 40;

    composer.redraw();

    expect(textarea.scrollTop).toBe(40);
  });

  it("keeps a draft typed at its end scrolled down through the redraw after the browser reveals the caret", () => {
    const textarea = makeOverflowingDraftTextarea();
    const composer = mountComposer(textarea);
    composer.type();
    // After the input event the browser scrolls the caret into view, short of the padding under it.
    textarea.scrollTop = DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX - 5;

    composer.redraw();

    expect(textarea.scrollTop).toBe(DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX);
    textarea.scrollTop = 40;
    composer.redraw();
    expect(textarea.scrollTop).toBe(40);
  });

  it("scrolls an overflowing draft all the way down while typing at its end", () => {
    const textarea = makeOverflowingDraftTextarea();
    const composer = mountComposer(textarea);
    // The browser has revealed the caret line, but not the padding under it.
    textarea.scrollTop = DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX - 5;

    composer.type();

    expect(textarea.scrollTop).toBe(DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX);
    const writeCountAtBottom = textarea.scrollTopWriteCount;
    composer.type();
    expect(textarea.scrollTopWriteCount).toBe(writeCountAtBottom);
  });

  it("keeps an overflowing draft's position while typing in its middle", () => {
    const textarea = makeOverflowingDraftTextarea({ caretPosition: 5 });
    const composer = mountComposer(textarea);
    textarea.scrollTop = 40;

    composer.type();

    expect(textarea.scrollTop).toBe(40);
  });

  it("scrolls an overflowing draft all the way down after picking a mention at its end", () => {
    mocks.isRoom = true;
    setMentionsForTesting([{ handle: "alice", display_name: "Alice", kind: "invited", key: "alice", chat_id: null }]);
    try {
      const textarea = makeOverflowingDraftTextarea({ draftSuffix: "@al" });
      const composer = mountComposer(textarea);
      composer.type();
      textarea.scrollTop = DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX - 5;

      composer.press("Enter");

      expect(textarea.value.endsWith("@alice ")).toBe(true);
      expect(textarea.scrollTop).toBe(DRAFT_CONTENT_PX - MAX_TEXTAREA_HEIGHT_PX);
    } finally {
      mocks.isRoom = false;
      setMentionsForTesting([]);
    }
  });
});

describe("MessageInput stop button during a compaction", () => {
  beforeEach(() => {
    mocks.agent.activity_state = "COMPACTING";
    localStorage.clear();
  });

  afterEach(() => {
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
  });

  it("shows for a harness whose interrupt cancels a compaction", () => {
    mocks.agent.harness = "claude";
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(rendered, "aria-label", "Interrupt agent")).toBeDefined();
  });

  it("is hidden for a harness whose interrupt does not, and back once its turn runs", () => {
    mocks.agent.harness = "codex";
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(render(), "aria-label", "Interrupt agent")).toBeUndefined();
    mocks.agent.activity_state = "THINKING";
    expect(findByAttr(render(), "aria-label", "Interrupt agent")).toBeDefined();
  });
});

/** The element tags and child positions from ``node`` down to the first ``tag``: what mithril matches an
 *  unkeyed element by from one render to the next. */
function elementPathTo(node: unknown, tag: string): string[] | null {
  if (node === null || node === undefined || typeof node !== "object" || Array.isArray(node)) return null;
  const vnode = node as AnyVnode;
  if (vnode.tag === tag) return [tag];
  if (!Array.isArray(vnode.children)) return null;
  for (const [index, child] of (vnode.children as unknown[]).entries()) {
    const below = elementPathTo(child, tag);
    if (below !== null) return [`${String(vnode.tag)}[${index}]`, ...below];
  }
  return null;
}

describe("MessageInput leading control", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("keeps the textarea where it was when a leading control comes or goes", () => {
    const component = MessageInput();
    const render = (leading: m.Children | undefined) =>
      component.view!({ attrs: { chatId: "agent-1", leading } } as never);
    const bare = elementPathTo(render(undefined), "textarea");
    expect(bare).not.toBeNull();
    expect(elementPathTo(render(m("button")), "textarea")).toEqual(bare);
  });
});

describe("MessageInput stop-to-composer handback", () => {
  beforeEach(() => {
    mocks.drainToComposer.mockReset();
    mocks.drainToComposer.mockResolvedValue({ block: "" });
    mocks.agent.harness = "claude";
    // Working -> the stop button is rendered.
    mocks.agent.activity_state = "THINKING";
    localStorage.clear();
  });

  function typeDraft(component: m.Component<{ chatId: string | null }>, chatId: string, text: string): void {
    const render = () => component.view!({ attrs: { chatId } } as never);
    const textarea = findByTag(render(), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)?.({ target: { value: text, style: {}, scrollHeight: 10 } });
  }

  async function clickStop(
    component: m.Component<{ chatId: string | null }>,
    chatId: string,
  ): Promise<AnyVnode | undefined> {
    const render = () => component.view!({ attrs: { chatId } } as never);
    // The label states what the press will do; with nothing queued (this mock
    // agent has no queued_messages) it reads as a plain interrupt.
    const stopButton = findByAttr(render(), "aria-label", "Interrupt agent");
    const onclick = stopButton?.attrs?.onclick as (() => Promise<void>) | undefined;
    expect(onclick, "stop button should be present while the agent works").toBeTruthy();
    await onclick!();
    return findByTag(render(), "textarea");
  }

  it("prepends the handed-back block above a non-empty draft", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one\nqueued two" });
    const component = MessageInput();
    typeDraft(component, "agent-1", "my draft");
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("queued one\nqueued two\n\nmy draft");
  });

  it("drops the block straight in when the composer is empty", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one" });
    const component = MessageInput();
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("queued one");
  });

  it("leaves a non-empty draft untouched on an empty handback", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "" });
    const component = MessageInput();
    typeDraft(component, "agent-1", "keep me");
    const textarea = await clickStop(component, "agent-1");
    expect(textarea?.attrs?.value).toBe("keep me");
  });
});

describe("MessageInput send to a chat still being created", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
  });

  afterEach(() => {
    mocks.whenChatRegistered.mockImplementation(async (_chatId: string) => {});
  });

  it("sends at once without waiting for the agent, which the chat app holds the message for", async () => {
    mocks.whenChatRegistered.mockImplementation(() => new Promise<void>(() => {}));

    await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    const [calledChatId, calledText] = mocks.sendMessage.mock.calls[0] as unknown as [string, string];
    expect(calledChatId).toBe("agent-1");
    expect(calledText).toContain("hello");
  });

  it("returns the message to the composer with the reason when the create has already failed", async () => {
    mocks.refuseSendToFailedChat.mockImplementationOnce(() => {
      throw new Error("mngr create exited with code 3");
    });

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("mngr create exited with code 3");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");
  });
});

describe("MessageInput first send of a seeded chat", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.launchChat.mockClear();
    mocks.openProviderChooser.mockReset();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
    // The seed is on the page and no agent exists yet: this send is what launches one.
    mocks.isChatRegistered = false;
    mocks.provisional = {
      chat_id: "agent-1",
      name: "Getting started",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: true,
      held_sends: [],
    };
    mocks.selectedAccount = { id: "acct-1" };
  });

  afterEach(() => {
    mocks.isChatRegistered = true;
    mocks.isLaunchInFlight = false;
    mocks.provisional = undefined;
    mocks.selectedAccount = null;
    mocks.accountsLoaded = true;
    mocks.whenChatRegistered.mockImplementation(async (_chatId: string) => {});
  });

  it("waits for the chat and sends to it when the sign-in's launch is already on its way, launching nothing itself", async () => {
    // The sign-in launched the chat a moment ago and the page has not yet heard it is creating.
    mocks.isLaunchInFlight = true;
    await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    expect(mocks.launchChat).not.toHaveBeenCalled();
    expect(mocks.whenChatRegistered).toHaveBeenCalledWith("agent-1");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "Let's build something", expect.stringMatching(/^m-/));
  });

  it("launches the chat on the signed-in account with the message as its first, and sends nothing else", async () => {
    await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    expect(mocks.launchChat).toHaveBeenCalledTimes(1);
    expect(mocks.launchChat).toHaveBeenCalledWith("agent-1", "acct-1", "Let's build something", "agent");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(mocks.openProviderChooser).not.toHaveBeenCalled();
  });

  it("asks the chooser for an account when none is signed in, and launches on the one it produces", async () => {
    // Sent before the account list loaded: the composer was there, and the send finds no account.
    mocks.accountsLoaded = false;
    mocks.selectedAccount = null;

    const sending = typeAndSend(MessageInput(), "agent-1", "Let's build something");
    await flushAsync();
    expect(mocks.launchChat).not.toHaveBeenCalled();
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onSignedIn: (accountId: string) => void };
    intent.onSignedIn("acct-2");
    await sending;

    expect(mocks.launchChat).toHaveBeenCalledWith("agent-1", "acct-2", "Let's build something", "agent");
  });

  it("launches the chat as a room, on no account, when the chooser's room row is picked instead", async () => {
    mocks.accountsLoaded = false;
    mocks.selectedAccount = null;

    const sending = typeAndSend(MessageInput(), "agent-1", "Let's build something");
    await flushAsync();
    expect(mocks.launchChat).not.toHaveBeenCalled();
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onRoomChosen: () => void };
    intent.onRoomChosen();
    await sending;

    expect(mocks.launchChat).toHaveBeenCalledWith("agent-1", "", "Let's build something", "room");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("puts the message back when the chooser is dismissed, launching nothing", async () => {
    mocks.accountsLoaded = false;
    mocks.selectedAccount = null;

    const sending = typeAndSend(MessageInput(), "agent-1", "Let's build something");
    await flushAsync();
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onDismissed: () => void };
    intent.onDismissed();
    await sending;

    expect(mocks.launchChat).not.toHaveBeenCalled();
    expect(localStorage.getItem("message-text:agent-1")).toContain("Let's build something");
  });

  it("puts the message back when the launch itself is refused", async () => {
    mocks.launchChat.mockRejectedValueOnce("Chat agent-1 is not waiting to be launched");

    const after = await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    expect(renderedText(after)).toContain("Couldn't send your message");
    expect(localStorage.getItem("message-text:agent-1")).toContain("Let's build something");
  });

  it("leaves the composer empty when the create fails after the launch took the message", async () => {
    // The chat keeps the message for the page's "Try again", which delivers it: a copy in the
    // composer would be sent twice.
    mocks.whenChatRegistered.mockRejectedValueOnce("mngr create exited with code 3");
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);

    const after = await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    expect(mocks.launchChat).toHaveBeenCalledTimes(1);
    expect(renderedText(after)).not.toContain("Couldn't send your message");
    expect(localStorage.getItem("message-text:agent-1") ?? "").not.toContain("Let's build something");
    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });
});

describe("MessageInput first send of a chat with no seed", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.launchChat.mockClear();
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
    mocks.isChatRegistered = false;
    mocks.provisional = {
      chat_id: "agent-1",
      name: "Chat 1",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: false,
      held_sends: [],
    };
    mocks.selectedAccount = { id: "acct-1" };
  });

  afterEach(() => {
    mocks.isChatRegistered = true;
    mocks.provisional = undefined;
    mocks.selectedAccount = null;
  });

  it("launches the chat with nothing, then sends the message, which the chat holds where Stop can reach it", async () => {
    await typeAndSend(MessageInput(), "agent-1", "Plan my week");

    expect(mocks.launchChat).toHaveBeenCalledWith("agent-1", "acct-1", "", "agent");
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage.mock.calls[0]).toEqual(expect.arrayContaining(["agent-1", "Plan my week"]));
  });

  it("drops the bubble of a first send that Stop took back, which no turn will retire", async () => {
    mocks.sendMessage.mockResolvedValueOnce("withdrawn");
    await typeAndSend(MessageInput(), "agent-1", "Taken back");

    expect(getOutgoingMessages("agent-1").map((outgoing) => outgoing.content)).not.toContain("Taken back");
  });
});

describe("MessageInput stop while connecting", () => {
  beforeEach(() => {
    mocks.drainToComposer.mockReset();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.agent.is_connecting = true;
    localStorage.clear();
  });

  afterEach(() => {
    mocks.agent.is_connecting = false;
  });

  it("offers Stop while a sent message waits for the agent, and puts the message back in the composer", async () => {
    mocks.drainToComposer.mockResolvedValueOnce({ block: "hello there" });
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);

    const stopButton = findByAttr(render(), "aria-label", "Stop sending and bring your message back to draft area");
    expect(stopButton, "Stop should be there while the chat connects").toBeDefined();
    await (stopButton?.attrs?.onclick as () => Promise<void>)();

    expect(mocks.drainToComposer).toHaveBeenCalledWith("agent-1");
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe("hello there");
  });

  it("offers no Stop when nothing is in progress", () => {
    mocks.agent.is_connecting = false;
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(
      findByAttr(rendered, "aria-label", "Stop sending and bring your message back to draft area"),
    ).toBeUndefined();
    expect(findByAttr(rendered, "aria-label", "Interrupt agent")).toBeUndefined();
  });

  it("drops the bubble of a send Stop took back, which no transcript turn will retire", async () => {
    mocks.agent.is_connecting = false;
    mocks.sendMessage.mockResolvedValueOnce("withdrawn");

    await typeAndSend(MessageInput(), "agent-1", "never mind");

    expect(getOutgoingMessages("agent-1")).toEqual([]);
  });
});

describe("MessageInput send failure notice", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.openProviderChooser.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
  });

  it("shows the reason the send was refused, in the workspace", async () => {
    // Rejecting with a plain string: describeRequestError is mocked as String(e), so an Error
    // would render as "Error: ...". mockRejectedValueOnce, not mockRejectedValue -- the mock is
    // module-wide and only cleared between tests, so a persistent rejection leaks into others.
    mocks.sendMessage.mockRejectedValueOnce("The agent is in shell mode with an unsubmitted command.");
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("shell mode with an unsubmitted command");
    // A failed send is recoverable, so it offers actions rather than a bare acknowledgement.
    expect(text).toContain("Cancel");
  });

  it("dismisses on OK, leaving the restored draft alone", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    const okButton = findByClass(after, "notice-dismiss");
    expect(okButton, "the notice should offer an OK button").toBeTruthy();
    (okButton!.attrs!.onclick as () => void)();
    const dismissed = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(dismissed)).not.toContain("Couldn't send your message");
  });

  it("does not follow the user to another agent", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "hello");
    const otherAgent = component.view!({ attrs: { chatId: "agent-2" } } as never);
    expect(renderedText(otherAgent)).not.toContain("Couldn't send your message");
  });

  it("refuses to send when an attachment failed to upload, and names it", async () => {
    // The failed upload is dropped from the message and its chip cleared, so without this the
    // file would vanish with no explanation.
    // Persistent, not Once: the view reads the attachments before handleSend does.
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.pdf", status: "error", error: "boom" },
    ]);
    const after = await typeAndSend(MessageInput(), "agent-1", "here you go");
    mocks.getComposerAttachments.mockReturnValue([]);
    const text = renderedText(after);
    expect(text).toContain("didn't upload");
    expect(text).toContain("notes.pdf");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("names the failed upload even over a send failure that landed during the upload wait", async () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.pdf", status: "error", error: "boom" },
    ]);
    const component = MessageInput();
    raiseFailureNotice("agent-1", { title: "Couldn't send your message", detail: "x", kind: "not_ready" });
    component.view!({ attrs: { chatId: "agent-1" } } as never);
    const after = await typeAndSend(component, "agent-1", "here you go");
    mocks.getComposerAttachments.mockReturnValue([]);
    expect(renderedText(after)).toContain("notes.pdf");
    expect(renderedText(after)).not.toContain("isn't ready for messages yet");
  });

  it("withholds Retry when the agent is unreachable, since it cannot help", async () => {
    // The chat app already tried the start once more, and Restart is the stronger way to bring it up.
    mocks.sendMessage.mockRejectedValueOnce({ kind: "agent_unreachable", toString: () => "start timed out" });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Restart");
    expect(text).not.toContain("Retry");
    expect(text).toContain("Restart it to send your message");
    expect(text).not.toContain("start timed out");
  });

  it("doesn't mention restarting on a notice that has no Restart button", async () => {
    raiseFailureNotice("agent-1", { title: "Couldn't send your message", detail: "x", kind: "agent_unreachable" });
    const text = renderedText(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("The agent isn't running and didn't start.");
    expect(text).not.toContain("Restart");
  });

  it("says what a send failure means in its own words, not mngr's", async () => {
    for (const [kind, sentence] of [
      ["input_blocked", "waiting on something in its terminal"],
      ["not_ready", "isn't ready for messages yet"],
      ["state_unknown", "Couldn't reach the agent just now"],
      ["agent_not_found", "Couldn't find the agent"],
      ["not_submitted", "didn't take your message"],
    ]) {
      mocks.sendMessage.mockRejectedValueOnce({ kind, toString: () => "mngr does not recognise this" });
      const after = await typeAndSend(MessageInput(), "agent-1", "hello");
      const text = renderedText(after);
      expect(text).toContain(sentence);
      expect(text).not.toContain("mngr");
    }
  });

  it("keeps the reason for a failure nothing classified, the only account of what went wrong", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "unknown", toString: () => "the host hung up" });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).toContain("the host hung up");
  });

  it("keeps Retry for a blocked input, which a person can clear", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "input_blocked", toString: () => "a dialog is open" });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Retry");
    expect(text).toContain("Restart");
  });

  it("shows no notice for a message the chat app delivered by sending it again", async () => {
    mocks.sendMessage.mockResolvedValueOnce("resent");
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).not.toContain("Couldn't confirm your message arrived");
    expect(renderedText(after)).not.toContain("Couldn't send your message");
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
  });

  it("asks the user to check the agent before sending an unconfirmed message again", async () => {
    // The chat app has already sent it twice without confirmation, and the agent may have either
    // copy: a plain Retry or a Restart (restart and resend) could deliver it a third time.
    // mngr's reason is diagnostics, not copy.
    mocks.sendMessage.mockRejectedValueOnce({
      kind: "submission_unconfirmed",
      toString: () => "Timeout waiting for message submission evidence: pane content: ❯ hello",
    });
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Couldn't confirm your message arrived");
    expect(text).toContain("The agent may already have it, so check its terminal before you send it again.");
    expect(text).not.toContain("Couldn't send your message");
    expect(text).not.toContain("Timeout waiting");
    expect(text).toContain("Cancel");
    expect(findButton(after, "Send again")).toBeTruthy();
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");
  });

  it("sends an unconfirmed message again only when the user asks", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    await flushAsync();
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);

    (findButton(after, "Send again")!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).toHaveBeenCalledTimes(2);
    expect(mocks.sendMessage).toHaveBeenLastCalledWith("agent-1", "hello", expect.stringMatching(/^m-/), true);
    const settled = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(settled)).not.toContain("Couldn't confirm your message arrived");
  });

  it("says what a sibling's send failure means in its own words too", () => {
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      detail: "mngr does not recognise this",
      kind: "not_ready",
      retry: vi.fn(async () => undefined),
    });
    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("isn't ready for messages yet");
    expect(text).not.toContain("mngr");
  });

  it("shows the new failure when a sibling's Retry fails differently", async () => {
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      detail: "first reason",
      kind: "not_ready",
      retry: vi.fn(async () => Promise.reject({ kind: "unknown", toString: () => "the host hung up" })),
    });
    const shown = component.view!({ attrs: { chatId: "agent-1" } } as never);
    (findButton(shown, "Retry")!.attrs!.onclick as () => void)();
    await flushAsync();
    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("the host hung up");
    expect(text).not.toContain("isn't ready for messages yet");
  });

  it("words a sibling's unconfirmed send the same way", () => {
    const retry = vi.fn(async () => undefined);
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      detail: "unconfirmed",
      kind: "submission_unconfirmed",
      retry,
    });
    const tree = component.view!({ attrs: { chatId: "agent-1" } } as never);
    const text = renderedText(tree);
    expect(text).toContain("Couldn't confirm your message arrived");
    expect(text).toContain("check its terminal before you send it again");
    expect(findButton(tree, "Retry")).toBeUndefined();

    (findButton(tree, "Send again")!.attrs!.onclick as () => void)();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("takes an unconfirmed message out of the composer and closes its notice once its turn lands", async () => {
    mocks.hasUserTurnLandedSince.mockClear();
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    expect(renderedText(after)).toContain("Couldn't confirm your message arrived");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");

    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const landed = component.view!({ attrs: { chatId: "agent-1" } } as never);

    // Looked for among the turns that were not there before the send, by the text that was sent.
    expect(mocks.hasUserTurnLandedSince).toHaveBeenLastCalledWith(
      "agent-1",
      "hello",
      new Set(["turn-before-the-send"]),
    );
    expect(renderedText(landed)).not.toContain("Couldn't confirm your message arrived");
    expect(findByTag(landed, "textarea")?.attrs?.value).toBe("");
    expect(localStorage.getItem("message-text:agent-1") ?? "").not.toContain("hello");
  });

  it("treats a send the chat app never answered as unconfirmed, and takes it out of the composer once its turn lands", async () => {
    // The chat app may have delivered it before its answer was lost: Force's restart could lose it,
    // and its repeat under the same id would be answered without sending it again.
    mocks.sendMessage.mockRejectedValueOnce({ code: 0, response: null, toString: () => "no answer" });
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    expect(renderedText(after)).toContain("Couldn't confirm your message arrived");
    expect(findButton(after, "Send again")).toBeTruthy();
    expect(findButton(after, "Force")).toBeUndefined();
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");

    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const landed = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(landed)).not.toContain("Couldn't confirm your message arrived");
    expect(findByTag(landed, "textarea")?.attrs?.value).toBe("");
  });

  it("still takes an unconfirmed message out of the composer when its turn lands after Cancel", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    (findByClass(after, "notice-dismiss")!.attrs!.onclick as () => void)();
    const dismissed = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByTag(dismissed, "textarea")?.attrs?.value).toBe("hello");

    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const landed = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByTag(landed, "textarea")?.attrs?.value).toBe("");
  });

  it("takes an unconfirmed message out of its chat's composer when its turn lands while another chat is shown", async () => {
    const component = MessageInput();
    const render = (chatId: string) => component.view!({ attrs: { chatId } } as never);
    mocks.sendMessage.mockImplementationOnce(async () => {
      // The user moves to another chat while the send is out.
      render("agent-2");
      throw { kind: "submission_unconfirmed", toString: () => "unconfirmed" };
    });
    const oninput = findByTag(render("agent-1"), "textarea")?.attrs?.oninput as (event: unknown) => void;
    oninput({ target: { value: "hello", style: {}, scrollHeight: 10 } });
    await (findByAttr(render("agent-1"), "aria-label", "Send message")!.attrs!.onclick as () => Promise<void>)();

    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    render("agent-2");
    expect(findByTag(render("agent-1"), "textarea")?.attrs?.value).toBe("");
    expect(localStorage.getItem("message-text:agent-1") ?? "").not.toContain("hello");
  });

  it("still takes an unconfirmed message out of its chat's composer after another chat's send was unconfirmed too", async () => {
    const component = MessageInput();
    const render = (chatId: string) => component.view!({ attrs: { chatId } } as never);
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    await typeAndSend(component, "agent-2", "goodbye");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");
    expect(localStorage.getItem("message-text:agent-2")).toContain("goodbye");

    mocks.hasUserTurnLandedSince.mockImplementation((chatId: string) => chatId === "agent-1");
    try {
      render("agent-2");
    } finally {
      mocks.hasUserTurnLandedSince.mockImplementation(() => false);
    }
    expect(localStorage.getItem("message-text:agent-1") ?? "").not.toContain("hello");
    expect(localStorage.getItem("message-text:agent-2")).toContain("goodbye");
  });

  it("shows no notice for an unconfirmed message whose turn landed before the failure came back", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).not.toContain("Couldn't confirm your message arrived");
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
  });

  it("leaves any other failed send's copy in the composer, whatever reaches the transcript", async () => {
    // Only an unconfirmed send is watched for its turn.
    mocks.hasUserTurnLandedSince.mockClear();
    mocks.sendMessage.mockRejectedValueOnce({ kind: "input_blocked", toString: () => "a dialog is open" });
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "hello");
    const later = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(mocks.hasUserTurnLandedSince).not.toHaveBeenCalled();
    expect(renderedText(later)).toContain("Couldn't send your message");
    expect(findByTag(later, "textarea")?.attrs?.value).toBe("hello");
  });

  it.each([
    ["nothing_to_compact", "Nothing to compact yet."],
    ["compact_unavailable", "The agent is busy. Send /compact again once it has finished."],
  ])(
    "shows a /compact the server declined (%s) as a declined command, keeping the typed text",
    async (kind, detail) => {
      mocks.sendMessage.mockRejectedValueOnce({ response: { detail, kind }, toString: () => detail });
      const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
      const component = MessageInput();

      const after = await typeAndSend(component, "agent-1", "/compact");

      const text = renderedText(after);
      expect(text).toContain("/compact wasn't sent");
      expect(text).toContain(detail);
      expect(text).not.toContain("Couldn't send your message");
      expect(text).not.toContain("Retry");
      expect(text).not.toContain("Restart");
      expect(text).not.toContain("terminal");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("/compact");
      expect(error).not.toHaveBeenCalled();
      error.mockRestore();

      (findByClass(after, "notice-dismiss")!.attrs!.onclick as () => void)();
      expect(renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never))).not.toContain(
        "/compact wasn't sent",
      );
    },
  );

  it("drops the Sending bubble of a /compact the server ran as a compaction, which no turn will echo", async () => {
    mocks.sendMessage.mockResolvedValueOnce("compaction_requested");

    const after = await typeAndSend(MessageInput(), "agent-compacted", "/compact");

    expect(getOutgoingMessages("agent-compacted")).toEqual([]);
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
    expect(renderedText(after)).not.toContain("wasn't sent");
  });

  it("keeps the Sending bubble of a delivered message until its turn arrives", async () => {
    mocks.sendMessage.mockResolvedValueOnce("ok");

    await typeAndSend(MessageInput(), "agent-delivered", "/compact");

    expect(getOutgoingMessages("agent-delivered").map((outgoing) => outgoing.content)).toEqual(["/compact"]);
  });

  it("paints the Sending bubble of an attachment-only send with the attachment, not an empty bubble", async () => {
    mocks.sendMessage.mockResolvedValueOnce("ok");
    mocks.getComposerAttachments.mockReturnValue([{ status: "ready", uploaded: { path: "/code/uploads/a/p.png" } }]);

    await typeAndSend(MessageInput(), "agent-image-only", "");
    mocks.getComposerAttachments.mockReturnValue([]);

    expect(getOutgoingMessages("agent-image-only").map((outgoing) => outgoing.content)).toEqual([
      " + /code/uploads/a/p.png",
    ]);
  });

  it("removes the delivered message even when Restart drained a queue block above it", async () => {
    // Restart prepends the rescued queue block BEFORE sending, so the delivered message is no
    // longer at the front of the composer -- a prefix-only strip would leave it there, sent and
    // still in the box.
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "my message");
    mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one" });

    const restart = findButton(after, "Restart");
    (restart!.attrs!.onclick as () => void)();
    await flushAsync();

    const composer = localStorage.getItem("message-text:agent-1") ?? "";
    expect(composer).toContain("queued one");
    expect(composer).not.toContain("my message");
  });

  it("shows nothing when the send succeeds", async () => {
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).not.toContain("Couldn't send your message");
  });

  it("shows a model pick the chat app refused in the chat's notice, with its reason", () => {
    const component = MessageInput();
    mocks.modelChoiceFailureListener!("agent-1", "This chat is switching to Claude 2; try again once it has.");

    const tree = component.view!({ attrs: { chatId: "agent-1" } } as never);

    const text = renderedText(tree);
    expect(text).toContain("Couldn't change the model");
    expect(text).toContain("This chat is switching to Claude 2; try again once it has.");
    expect(text).not.toContain("[object Object]");
    expect(text).not.toContain("Retry");
  });

  it("offers Cancel, Retry and Restart for a failed send", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Cancel");
    expect(text).toContain("Retry");
    expect(text).toContain("Restart");
  });

  it("puts the message back in the composer immediately, not only on Cancel", async () => {
    // The recovery record is closure state, so holding the message only there would lose it on
    // a reload or a closed tab. It is persisted the moment the send fails (contract A1a).
    mocks.sendMessage.mockRejectedValueOnce("nope");
    await typeAndSend(MessageInput(), "agent-1", "hello there");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello there");
  });

  it("prepends the failed message above a draft typed while the send was in flight", async () => {
    // The newer draft lands in storage while the request is in flight, which is exactly the
    // case the old "restore only into an empty composer" guard existed for.
    mocks.sendMessage.mockImplementationOnce(async () => {
      localStorage.setItem("message-text:agent-1", "newer draft");
      throw "nope";
    });
    await typeAndSend(MessageInput(), "agent-1", "failed message");
    const restored = localStorage.getItem("message-text:agent-1") ?? "";
    expect(restored).toContain("failed message");
    expect(restored).toContain("newer draft");
    expect(restored.indexOf("failed message")).toBeLessThan(restored.indexOf("newer draft"));
  });

  it("retries the same message through the ordinary send", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();

    const retry = findButton(after, "Retry");
    (retry!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello", expect.stringMatching(/^m-/), true);
  });

  it("Restart restarts the agent before sending, in that order", async () => {
    mocks.sendMessage.mockRejectedValueOnce("nope");
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();
    mocks.interruptAgent.mockClear();

    const restart = findButton(after, "Restart");
    (restart!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.drainToComposer).toHaveBeenCalledWith("agent-1");
    expect(mocks.interruptAgent).toHaveBeenCalledWith("agent-1");
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "hello", expect.stringMatching(/^m-/), true);
  });

  it("does not send when the restart itself fails, and shows the restart's error", async () => {
    mocks.sendMessage.mockRejectedValueOnce({ kind: "input_blocked", toString: () => "a dialog is open" });
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockClear();
    mocks.interruptAgent.mockRejectedValueOnce("restart refused");

    const restart = findButton(after, "Restart");
    (restart!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const shown = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(shown)).toContain("restart refused");
  });
});

describe("MessageInput switching harness", () => {
  const TARGET = { id: "acct-openai", harness: "codex", label: "OpenAI (Codex)" };
  const PICK = { identity: { model_id: "gpt-6-astra", effort: "high", fast: false }, label: "GPT-6 Astra · High" };

  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.clearComposerAttachments.mockClear();
    mocks.switchChat.mockClear();
    mocks.switchChat.mockResolvedValue({ kind: "handoff", phase: "summarizing", returned_block: "" });
    mocks.setPendingAccount.mockClear();
    mocks.openSwitchDialog.mockClear();
    mocks.cancelHandoff.mockClear();
    mocks.cancelHandoff.mockResolvedValue({ returned_block: "" });
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.switching.handoff = null;
    mocks.switching.target = null;
    mocks.switching.pick = null;
    mocks.switching.kind = "handoff";
    localStorage.clear();
  });

  afterEach(() => {
    mocks.switching.handoff = null;
    mocks.switching.target = null;
    mocks.switching.pick = null;
    mocks.switching.kind = "handoff";
  });

  function typeDraft(component: m.Component<{ chatId: string | null }>, chatId: string, text: string): unknown {
    const render = () => component.view!({ attrs: { chatId } } as never);
    const textarea = findByTag(render(), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)?.({ target: { value: text, style: {}, scrollHeight: 10 } });
    return render();
  }

  /** Press a rendered button (a notice's action or the composer's own). */
  function press(button: AnyVnode | undefined): void {
    expect(button, "the button should be rendered").toBeTruthy();
    (button!.attrs!.onclick as () => void)();
  }

  it("reads Switch and send while a switch is armed, and says what the next message does", () => {
    mocks.switching.target = TARGET;
    mocks.switching.pick = PICK;
    const rendered = typeDraft(MessageInput(), "agent-1", "Carry on in Codex");
    expect(findByAttr(rendered, "aria-label", "Send message")).toBeUndefined();
    expect(renderedText(findByAttr(rendered, "aria-label", "Switch and send"))).toContain("Switch and send");
    const strip = findByClass(rendered, "message-input-switch-strip");
    expect(renderedText(strip)).toContain(
      "Your next message switches this chat to OpenAI (Codex), GPT-6 Astra · High",
    );
    // Nothing has gone out: the switch waits for the send.
    expect(mocks.switchChat).not.toHaveBeenCalled();
  });

  it("carries the armed switch out on send, with the pick, and puts the returned queue back in the composer", async () => {
    mocks.switching.target = TARGET;
    mocks.switching.pick = PICK;
    mocks.switchChat.mockResolvedValueOnce({ kind: "handoff", phase: "summarizing", returned_block: "queued one" });
    const component = MessageInput();
    const rendered = typeDraft(component, "agent-1", "Carry on in Codex");
    press(findByAttr(rendered, "aria-label", "Switch and send"));
    await flushAsync();

    expect(mocks.switchChat).toHaveBeenCalledTimes(1);
    const [chatId, accountId, message, messageId, pick] = mocks.switchChat.mock.calls[0] as unknown as unknown[];
    expect([chatId, accountId, message]).toEqual(["agent-1", "acct-openai", "Carry on in Codex"]);
    expect(messageId).toMatch(/^m-/);
    expect(pick).toEqual(PICK.identity);
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("queued one");
  });

  it("sends the harness's default when no model was picked", async () => {
    mocks.switching.target = TARGET;
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();
    const [, , , , pick] = mocks.switchChat.mock.calls[0] as unknown as unknown[];
    expect(pick).toBeNull();
    const strip = findByClass(typeDraft(component, "agent-1", "x"), "message-input-switch-strip");
    expect(renderedText(strip)).toContain("switches this chat to OpenAI (Codex)");
    expect(renderedText(strip)).not.toContain("OpenAI (Codex),");
  });

  it("puts the message back and says why when the switch is refused", async () => {
    mocks.switching.target = TARGET;
    mocks.switchChat.mockRejectedValueOnce(new Error("Chat already runs on account acct-openai"));
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();

    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(after)).toContain("Couldn't switch to Codex");
    expect(renderedText(after)).toContain("Chat already runs on account acct-openai");
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("Carry on in Codex");
  });

  it("offers the dialog again from the strip, and a way to call the choice off", () => {
    mocks.switching.target = TARGET;
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    press(findByClass(rendered, "message-input-switch-change"));
    expect(mocks.openSwitchDialog).toHaveBeenCalledWith("agent-1", TARGET);
    press(findByClass(rendered, "message-input-switch-cancel"));
    expect(mocks.setPendingAccount).toHaveBeenCalledWith("agent-1", null);
  });

  it("offers the dialog for an armed rebind too, which was armed without one, and carries the pick made there", async () => {
    const target = { id: "acct-anthropic-2", harness: "claude", label: "Anthropic 2 (Claude Code)" };
    mocks.switching.target = target;
    mocks.switching.kind = "rebind";
    const component = MessageInput();
    const rendered = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(findByClass(rendered, "message-input-switch-strip"))).toContain(
      "switches this chat to Anthropic 2 (Claude Code)",
    );
    press(findByClass(rendered, "message-input-switch-change"));
    expect(mocks.openSwitchDialog).toHaveBeenCalledWith("agent-1", target);

    mocks.switching.pick = { identity: { model_id: "haiku", effort: "low", fast: false }, label: "Haiku 4.5 · Low" };
    const armed = typeDraft(component, "agent-1", "Carry on here");
    expect(renderedText(findByClass(armed, "message-input-switch-strip"))).toContain(
      "switches this chat to Anthropic 2 (Claude Code), Haiku 4.5 · Low",
    );
    press(findByAttr(armed, "aria-label", "Switch and send"));
    await flushAsync();
    const [, accountId, , , pick] = mocks.switchChat.mock.calls[0] as unknown as unknown[];
    expect(accountId).toBe("acct-anthropic-2");
    expect(pick).toEqual({ model_id: "haiku", effort: "low", fast: false });
  });

  it("says nothing more about the switch once its message has gone, and holds a second send until it is answered", async () => {
    mocks.switching.target = TARGET;
    let answer: (value: { kind: string; phase: string; returned_block: string }) => void = () => undefined;
    mocks.switchChat.mockReturnValueOnce(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();
    expect(mocks.switchChat).toHaveBeenCalledTimes(1);

    // The message is on its way: "your next message switches" would now be about the one after it.
    const sending = typeDraft(component, "agent-1", "And one more thing");
    expect(findByClass(sending, "message-input-switch-strip")).toBeUndefined();
    expect(findByAttr(sending, "aria-label", "Switch and send")).toBeUndefined();
    const heldSend = findByAttr(sending, "aria-label", "Send message");
    expect(heldSend?.attrs?.["aria-disabled"]).toBe("true");
    press(heldSend);
    await flushAsync();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(mocks.switchChat).toHaveBeenCalledTimes(1);
    expect(findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea")?.attrs?.value).toBe(
      "And one more thing",
    );

    answer({ kind: "handoff", phase: "summarizing", returned_block: "" });
    await flushAsync();
    mocks.switching.handoff = handoffStateFixture({ phase: "summarizing" });
    const running = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByClass(running, "message-input-switch-strip")).toBeUndefined();
  });

  it("keeps the switch the next send's while its message waits on an upload", async () => {
    mocks.switching.target = TARGET;
    const release = holdComposerUploads();
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();

    // Nothing has gone yet: the draft is still the message that switches the chat.
    const waiting = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByClass(waiting, "message-input-switch-strip")).toBeDefined();
    press(findByAttr(waiting, "aria-label", "Switch and send"));
    await flushAsync();
    expect(mocks.switchChat).not.toHaveBeenCalled();

    release();
    await flushAsync();
    expect(mocks.switchChat).toHaveBeenCalledTimes(1);
  });

  it("carries out the choice as it stands once the message is ready, and none if it was taken back", async () => {
    const component = MessageInput();

    // Changed while the message waited: the account and its pick go out together.
    mocks.switching.target = TARGET;
    let release = holdComposerUploads();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on"), "aria-label", "Switch and send"));
    await flushAsync();
    const other = { id: "acct-anthropic-2", harness: "claude", label: "Anthropic 2 (Claude Code)" };
    mocks.switching.target = other;
    mocks.switching.pick = { identity: { model_id: "haiku", effort: "low", fast: false }, label: "Haiku 4.5 · Low" };
    release();
    await flushAsync();
    const [, accountId, , , pick] = mocks.switchChat.mock.calls[0] as unknown as unknown[];
    expect(accountId).toBe("acct-anthropic-2");
    expect(pick).toEqual({ model_id: "haiku", effort: "low", fast: false });

    // Taken back while the message waited: nothing goes, and the message is the user's again.
    mocks.switchChat.mockClear();
    mocks.switching.target = TARGET;
    release = holdComposerUploads();
    press(findByAttr(typeDraft(component, "agent-1", "Never mind"), "aria-label", "Switch and send"));
    await flushAsync();
    mocks.switching.target = null;
    release();
    await flushAsync();
    expect(mocks.switchChat).not.toHaveBeenCalled();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea")?.attrs?.value).toBe(
      "Never mind",
    );
  });

  it("shows no strip while the switch is already running", () => {
    mocks.switching.target = TARGET;
    mocks.switching.handoff = handoffStateFixture({ phase: "summarizing" });
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByClass(rendered, "message-input-switch-strip")).toBeUndefined();
  });

  it("offers Cancel switch instead of Stop while the switch can still be called off, and returns the trigger", async () => {
    mocks.agent.activity_state = "THINKING";
    mocks.switching.handoff = handoffStateFixture({ phase: "summarizing" });
    mocks.cancelHandoff.mockResolvedValueOnce({ returned_block: "Carry on in Codex" });
    const component = MessageInput();
    const rendered = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(rendered, "aria-label", "Interrupt agent")).toBeUndefined();
    expect(findByTag(rendered, "textarea")?.attrs?.placeholder).toBe(
      "Type a message; it is delivered once Codex is ready…",
    );
    const cancel = findByAttr(rendered, "aria-label", "Cancel switch");
    expect(renderedText(cancel)).toContain("Cancel switch");
    (cancel?.attrs?.onclick as () => void)();
    await flushAsync();
    expect(mocks.cancelHandoff).toHaveBeenCalledWith("agent-1");
    expect(findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea")?.attrs?.value).toBe(
      "Carry on in Codex",
    );
  });

  it("offers neither Stop nor Cancel switch once the old agent is being replaced", () => {
    mocks.agent.activity_state = "THINKING";
    mocks.switching.handoff = handoffStateFixture({ phase: "switching" });
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(rendered, "aria-label", "Interrupt agent")).toBeUndefined();
    expect(findByAttr(rendered, "aria-label", "Cancel switch")).toBeUndefined();
  });

  it("gives its draft up to a sibling that takes it, attachments and all, and comes back empty", async () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "plan.pdf", status: "ready", uploaded: { path: "/uploads/plan.pdf" } },
    ]);
    const component = MessageInput();
    typeDraft(component, "agent-1", "moving house");
    const taken = await takeComposerDraft("agent-1");
    mocks.getComposerAttachments.mockReturnValue([]);
    expect(taken).toEqual({
      text: "moving house",
      // The mocked builder appends the ready paths it was given.
      finalText: "moving house + /uploads/plan.pdf",
      attachments: [{ localId: "a1", fileName: "plan.pdf", status: "ready", uploaded: { path: "/uploads/plan.pdf" } }],
      mayContainDictatedSpeech: false,
    });
    // The chips go with the text, rather than being left behind in a composer it emptied.
    expect(mocks.clearComposerAttachments).toHaveBeenCalledWith("agent-1");
    expect(localStorage.getItem("message-text:agent-1")).toBeNull();
    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
  });

  it("refuses to give its draft up while an attachment has failed to upload", async () => {
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "notes.pdf", status: "error", error: "boom" },
    ]);
    const component = MessageInput();
    typeDraft(component, "agent-1", "moving house");
    expect(await takeComposerDraft("agent-1")).toBeNull();
    mocks.getComposerAttachments.mockReturnValue([]);
    // Nothing was taken: the file would have left the message silently and its chip with it.
    expect(localStorage.getItem("message-text:agent-1")).toBe("moving house");
    expect(mocks.clearComposerAttachments).not.toHaveBeenCalled();
    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("didn't upload");
    expect(text).toContain("notes.pdf");
  });

  it("keeps a draft handed straight back after a sibling took it", async () => {
    const component = MessageInput();
    typeDraft(component, "agent-1", "moving house");
    const taken = await takeComposerDraft("agent-1");
    // No view pass in between: the take and the hand-back are both pending on the next one.
    restoreComposerDraft("agent-1", taken!);
    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("moving house");
  });

  it("sends ordinarily, with the send-time id, when no lane is pending", async () => {
    await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    const [, text, messageId] = mocks.sendMessage.mock.calls[0] as unknown as string[];
    expect(text).toBe("hello");
    expect(messageId).toMatch(/^m-/);
  });
});

describe("setComposerHint", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  it("shows the hint as the placeholder until a send, and an empty hint is nothing", async () => {
    const component = MessageInput();
    setComposerHint("agent-1", "How do you want to change Docs?");
    expect(localStorage.getItem("composer-hint:agent-1")).toBe("How do you want to change Docs?");
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("How do you want to change Docs?");
    // Another chat's composer keeps its own words.
    const other = findByTag(component.view!({ attrs: { chatId: "agent-2" } } as never), "textarea");
    expect(other?.attrs?.placeholder).toBe("Type a message...");
    setComposerHint("agent-3", "");
    expect(localStorage.getItem("composer-hint:agent-3")).toBeNull();
    const unhinted = findByTag(component.view!({ attrs: { chatId: "agent-3" } } as never), "textarea");
    expect(unhinted?.attrs?.placeholder).toBe("Type a message...");

    // The send answers the hint's question: the placeholder goes back to the composer's own words, here and
    // for a composer that mounts later.
    const sent = await typeAndSend(component, "agent-1", "Make it blue");
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(findByTag(sent, "textarea")?.attrs?.placeholder).toBe("Type a message...");
    expect(localStorage.getItem("composer-hint:agent-1")).toBeNull();
  });

  it("is taken up by a composer that mounts after the chat root persisted it", () => {
    // The root's document sets the hint before the chat's page has loaded; the page's composer, a document of its
    // own, finds it persisted when it first views the chat.
    localStorage.setItem("composer-hint:agent-4", "How do you want to change Notes?");
    const textarea = findByTag(MessageInput().view!({ attrs: { chatId: "agent-4" } } as never), "textarea");
    expect(textarea?.attrs?.placeholder).toBe("How do you want to change Notes?");
  });
});

describe("prependToComposer with an element reference", () => {
  const REFERENCE_ID = "REF-aaaaaaaaaaa";
  // The fields the chat reads off a reference; the rest of a real one locates nothing here.
  const BLOCK = jsonBlock({
    element_reference: {
      reference_id: REFERENCE_ID,
      tag: "button",
      id: "save",
      classes: ["primary"],
      app: "docs",
      page_path: "/intro",
    },
  });

  beforeEach(() => {
    localStorage.clear();
    mocks.uploadDescribedFileToComposer.mockClear();
  });

  function stagedFile(): { chatId: string; file: File; summary: string } {
    expect(mocks.uploadDescribedFileToComposer).toHaveBeenCalledTimes(1);
    const [chatId, file, summary] = mocks.uploadDescribedFileToComposer.mock.calls[0] as [string, File, string];
    return { chatId, file, summary };
  }

  it("attaches the block as the reference's file and keeps only the prompt as text, above the draft", () => {
    const component = MessageInput();
    localStorage.setItem("message-text:agent-1", "my draft");
    prependToComposer("agent-1", `Explain what I attached in ${REFERENCE_ID}\n\n${BLOCK}`);
    expect(localStorage.getItem("message-text:agent-1")).toBe(
      `Explain what I attached in ${REFERENCE_ID}\n\nmy draft`,
    );
    const { chatId, file, summary } = stagedFile();
    expect(chatId).toBe("agent-1");
    expect(file.name).toBe(`${REFERENCE_ID}.json`);
    expect(summary).toBe("button#save.primary in docs /intro");
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    expect(textarea?.attrs?.value).toBe(`Explain what I attached in ${REFERENCE_ID}\n\nmy draft`);
  });

  it("stages a block on its own and leaves the draft text alone", () => {
    localStorage.setItem("message-text:agent-1", "my draft");
    prependToComposer("agent-1", BLOCK);
    expect(localStorage.getItem("message-text:agent-1")).toBe("my draft");
    expect(stagedFile().file.name).toBe(`${REFERENCE_ID}.json`);
  });

  it("prepends a block once and not again while it still leads the draft (the app-chat button pressed twice)", () => {
    prependToComposer("agent-1", "Change something about the Docs app: ");
    prependToComposer("agent-1", "Change something about the Docs app: ");
    expect(localStorage.getItem("message-text:agent-1")).toBe("Change something about the Docs app: ");
  });
});

describe("MessageInput on a chat awaiting its first send with nothing signed in", () => {
  beforeEach(() => {
    mocks.openProviderChooser.mockReset();
    mocks.isChatRegistered = false;
    mocks.provisional = {
      chat_id: "agent-first-7731",
      name: "Welcome",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: true,
      held_sends: [],
    };
    mocks.selectedAccount = null;
    localStorage.clear();
  });

  afterEach(() => {
    mocks.isChatRegistered = true;
    mocks.provisional = undefined;
    mocks.selectedAccount = null;
    mocks.accountsLoaded = true;
  });

  it("disables the composer under a way to connect a provider", () => {
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

    expect(renderedText(tree)).toContain("Connect an AI provider to start chatting.");
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
    // Send keeps its place opposite the disabled paperclip, greyed out.
    expect(findByAttr(tree, "aria-label", "Send message")?.attrs?.disabled).toBe(true);
    const chooseButton = findButton(tree, "Choose a provider");
    expect(chooseButton).toBeDefined();
    (chooseButton!.attrs!.onclick as () => void)();
    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
  });

  it("opens the provider chooser as the notice appears", () => {
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

    const notice = findByClass(tree, "message-input-no-provider");
    (notice!.attrs!.oncreate as () => void)();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
  });

  it("launches the awaiting chat with no message as the chooser's sign-in completes, from the button or on appearing", async () => {
    mocks.launchChat.mockClear();
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);
    (findByClass(tree, "message-input-no-provider")!.attrs!.oncreate as () => void)();
    (findButton(tree, "Choose a provider")!.attrs!.onclick as () => void)();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(2);
    for (const call of mocks.openProviderChooser.mock.calls) {
      const intent = call[0] as { onSignedIn: (accountId: string) => void };
      intent.onSignedIn("acct-9");
    }
    await Promise.resolve();

    // The agent is up before anything is typed; the first send then carries the seeded conversation.
    expect(mocks.launchChat).toHaveBeenCalledTimes(2);
    expect(mocks.launchChat).toHaveBeenCalledWith("agent-first-7731", "acct-9", "", "agent");
  });

  it("launches nothing on sign-in once the chat is no longer awaiting its first send or is already listed", () => {
    mocks.launchChat.mockClear();
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);
    (findButton(tree, "Choose a provider")!.attrs!.onclick as () => void)();
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onSignedIn: (accountId: string) => void };

    // A send in the meantime launched it: the provisional chat is creating.
    mocks.provisional = { ...(mocks.provisional as object), phase: "creating" };
    intent.onSignedIn("acct-9");
    expect(mocks.launchChat).not.toHaveBeenCalled();

    // The page's own launch is on its way and the backend has not said so yet.
    mocks.provisional = { ...(mocks.provisional as object), phase: "awaiting_first_send" };
    mocks.isLaunchInFlight = true;
    intent.onSignedIn("acct-9");
    expect(mocks.launchChat).not.toHaveBeenCalled();
    mocks.isLaunchInFlight = false;

    // The create landed: the chat list names it.
    mocks.provisional = { ...(mocks.provisional as object), phase: "awaiting_first_send" };
    mocks.isChatRegistered = true;
    intent.onSignedIn("acct-9");
    expect(mocks.launchChat).not.toHaveBeenCalled();
  });

  it("launches the chat as a room, with nothing said yet, when the notice's chooser picks the room row", async () => {
    mocks.launchAwaitingChatAsRoom.mockClear();
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);
    (findButton(tree, "Choose a provider")!.attrs!.onclick as () => void)();

    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onRoomChosen: () => void };
    intent.onRoomChosen();
    await flushAsync();

    expect(mocks.launchAwaitingChatAsRoom).toHaveBeenCalledExactlyOnceWith("agent-first-7731");
  });

  it("keeps the phone's leading settings button beside the notice", () => {
    const leading = m("button", { "aria-label": "Chat settings" });
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731", leading } } as never);

    expect(findByAttr(tree, "aria-label", "Chat settings")).toBeDefined();
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
  });

  it("gives the composer back once an account is signed in", () => {
    const component = MessageInput();
    expect(
      findByTag(component.view!({ attrs: { chatId: "agent-first-7731" } } as never), "textarea")?.attrs?.disabled,
    ).toBe(true);

    mocks.selectedAccount = { id: "acct-7732" };
    const tree = component.view!({ attrs: { chatId: "agent-first-7731" } } as never);

    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(false);
    expect(renderedText(tree)).not.toContain("Connect an AI provider");
  });

  it("shows the composer while the account list has not loaded yet", () => {
    mocks.accountsLoaded = false;
    const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(false);
    expect(renderedText(tree)).not.toContain("Connect an AI provider");
  });
});

describe("MessageInput on a chat whose account was signed out", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.openProviderChooser.mockClear();
    mocks.beginSwitchToAccountId.mockClear();
    mocks.loadAccounts.mockClear();
    mocks.agent.harness = "codex";
    mocks.agent.activity_state = undefined;
    mocks.agent.account_id = "account-signed-out-5521";
    mocks.signedOutAccountIds.clear();
    mocks.signedOutAccountIds.add("account-signed-out-5521");
    mocks.switching.target = null;
    mocks.switching.handoff = null;
    localStorage.clear();
  });

  afterEach(() => {
    mocks.agent.account_id = null;
    mocks.signedOutAccountIds.clear();
  });

  it("disables the composer under the reason and a way to choose another provider", () => {
    const component = MessageInput();
    const tree = component.view!({ attrs: { chatId: "agent-signed-out" } } as never);

    expect(renderedText(tree)).toContain("You signed out of the account this chat runs on.");
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
    const chooseButton = findButton(tree, "Choose a provider");
    expect(chooseButton).toBeDefined();

    (chooseButton!.attrs!.onclick as () => void)();
    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onSignedIn: (accountId: string) => void };
    intent.onSignedIn("account-chosen-5522");
    expect(mocks.beginSwitchToAccountId).toHaveBeenCalledWith("agent-signed-out", "account-chosen-5522");
  });

  it("keeps the phone's leading settings button under the signed-out notice", () => {
    const component = MessageInput();
    const leading = m("button", { "aria-label": "Chat settings" });
    const tree = component.view!({ attrs: { chatId: "agent-signed-out", leading } } as never);

    expect(findByAttr(tree, "aria-label", "Chat settings")).toBeDefined();
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
    expect(findButton(tree, "Choose a provider")).toBeDefined();
  });

  it("gives the composer back once a switch to another provider is armed", () => {
    mocks.switching.target = { id: "account-chosen-5522", harness: "claude", label: "Claude" };
    const component = MessageInput();
    const tree = component.view!({ attrs: { chatId: "agent-signed-out" } } as never);

    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(false);
    expect(renderedText(tree)).not.toContain("You signed out of the account this chat runs on.");
  });

  it("keeps the composer while the switch away from it is being sent", async () => {
    mocks.switching.target = { id: "account-chosen-5522", harness: "claude", label: "Claude" };
    let answer: (value: { kind: string; phase: string; returned_block: string }) => void = () => undefined;
    mocks.switchChat.mockReturnValueOnce(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-signed-out" } } as never);
    const textarea = findByTag(render(), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)({
      target: { value: "Carry on in Claude", style: {}, scrollHeight: 10 },
    });
    (findByAttr(render(), "aria-label", "Switch and send")!.attrs!.onclick as () => void)();
    await vi.waitFor(() => expect(mocks.switchChat).toHaveBeenCalledTimes(1));

    const sending = render();
    expect(findByTag(sending, "textarea")?.attrs?.disabled).toBe(false);
    expect(renderedText(sending)).not.toContain("You signed out of the account this chat runs on.");

    answer({ kind: "handoff", phase: "summarizing", returned_block: "" });
    await flushAsync();
  });

  it("catches up with a sign-out made in another window when the send is refused for it", async () => {
    // This page's list still has the account, so the composer is there to send from.
    mocks.signedOutAccountIds.clear();
    mocks.loadAccounts.mockImplementationOnce(async () => {
      mocks.signedOutAccountIds.add("account-signed-out-5521");
    });
    mocks.sendMessage.mockRejectedValueOnce({
      response: { kind: "account_signed_out" },
      toString: () => "You signed out of the account this chat runs on.",
    });
    const component = MessageInput();

    await typeAndSend(component, "agent-signed-out", "are you still there?");

    await vi.waitFor(() => expect(mocks.loadAccounts).toHaveBeenCalledTimes(1));
    const tree = component.view!({ attrs: { chatId: "agent-signed-out" } } as never);
    expect(findButton(tree, "Choose a provider")).toBeDefined();
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
  });

  it("offers choosing a provider instead of Retry and Restart when a send is refused for a signed-out account", async () => {
    mocks.signedOutAccountIds.clear();
    mocks.sendMessage.mockRejectedValueOnce({
      response: { kind: "account_signed_out" },
      toString: () => "You signed out of the account this chat runs on. Choose a provider to continue it on.",
    });
    const component = MessageInput();

    const after = await typeAndSend(component, "agent-signed-out", "are you still there?");

    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("You signed out of the account this chat runs on.");
    expect(text).not.toContain("Retry");
    expect(text).not.toContain("Restart");
    expect(text).not.toContain("open the agent's terminal");
    (findButton(after, "Choose a provider")!.attrs!.onclick as () => void)();
    const dismissed = component.view!({ attrs: { chatId: "agent-signed-out" } } as never);
    expect(renderedText(dismissed)).not.toContain("Couldn't send your message");
    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    const intent = mocks.openProviderChooser.mock.calls[0][0] as { onSignedIn: (accountId: string) => void };
    intent.onSignedIn("account-chosen-5522");
    expect(mocks.beginSwitchToAccountId).toHaveBeenCalledWith("agent-signed-out", "account-chosen-5522");
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("message-text:agent-signed-out")).toContain("are you still there?");
  });

  it("leaves the account list alone when a send fails for any other reason", async () => {
    mocks.signedOutAccountIds.clear();
    mocks.sendMessage.mockRejectedValueOnce({
      response: { kind: "input_blocked" },
      toString: () => "a dialog is open",
    });

    await typeAndSend(MessageInput(), "agent-signed-out", "hello");

    expect(mocks.loadAccounts).not.toHaveBeenCalled();
  });
});

describe("MessageInput dictation", () => {
  const CHAT = "agent-dictating";
  const LOAD_KEY = ["speech-model", "moonshine-base", null];
  const PROGRESS_KEY = ["speech-model-progress", "moonshine-base", null];
  const LOADED = { device: "wasm" } as const;

  /** The page's speech models over the test's query client, standing in for the worker. */
  function fakeSpeechModels(): FakeSpeechModels {
    const loads: FakeSpeechModels["loads"] = [];
    let finishDiscovery: () => void = () => undefined;
    const discovered = new Promise<void>((resolve) => {
      finishDiscovery = resolve;
    });
    const loadQueryOptions = () => ({
      queryKey: LOAD_KEY,
      queryFn: () =>
        new Promise<{ device: "wasm" }>((resolve, reject) => {
          loads.push({ resolve, reject });
        }),
    });
    return {
      loads,
      finishDiscovery: () => finishDiscovery(),
      loadQueryOptions,
      progressQueryKey: () => PROGRESS_KEY,
      request: vi.fn(() => mocks.voiceQueryClient.query(loadQueryOptions())),
      discover: vi.fn(() => discovered),
    };
  }

  beforeEach(async () => {
    mocks.sendMessage.mockClear();
    mocks.isDictationSupported = true;
    mocks.dictation = null;
    mocks.dictationEvents = null;
    mocks.dictationDeviceClasses = [];
    mocks.screenWakeWanted = [];
    mocks.speechModelProbe.mockResolvedValue({ isDownloaded: true, totalBytes: 250_000_000, device: "wasm" });
    mocks.readyChime.wake.mockClear();
    mocks.readyChime.play.mockClear();
    const { createVoiceQueryClient } = await vi.importActual<typeof import("@imbue/workspace-voice/src/speechModel")>(
      "@imbue/workspace-voice/src/speechModel",
    );
    mocks.voiceQueryClient = createVoiceQueryClient();
    mocks.speechModels = fakeSpeechModels();
    localStorage.clear();
  });

  // What takes each composer a test put on the page off it again, so none goes on waiting for the model's state
  // into the tests that follow.
  const leavePage: (() => void)[] = [];

  afterEach(() => {
    for (const leave of leavePage.splice(0)) leave();
    mocks.isDictationSupported = false;
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  function unmountedComposer() {
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: CHAT } } as never);
    return { component, render };
  }

  /** Put the composer on the page as mounting does, which is when it starts to follow the page's speech model.
   *  The page's window is a stub the dictation hooks can register on. */
  function putOnPage(render: () => unknown): void {
    vi.stubGlobal("window", { addEventListener: () => undefined, removeEventListener: () => undefined });
    const root = findByClass(render(), "message-input mx-auto")!;
    (root.attrs!.oncreate as (() => void) | undefined)?.();
    const leave = root.attrs!.onremove as (() => void) | undefined;
    if (leave !== undefined) leavePage.push(leave);
  }

  /** A composer on a page whose speech model is loaded, as on one that has dictated before: the mic dictates at
   *  the press. */
  function composer() {
    mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
    const mounted = unmountedComposer();
    putOnPage(mounted.render);
    return mounted;
  }

  /** Let everything a settled promise sets off run: the query's own steps, then the composer's. */
  async function settled(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  /** A composer on a page whose speech model is not loaded, once the page knows it: the probe has answered
   *  (``isDownloaded``), and so has discovery unless ``isDiscovered`` is false. */
  async function composerWithoutModel({ isDownloaded = false, isDiscovered = true } = {}) {
    mocks.speechModelProbe.mockResolvedValue({ isDownloaded, totalBytes: 250_000_000, device: "wasm" });
    const mounted = unmountedComposer();
    putOnPage(mounted.render);
    await settled();
    if (isDiscovered) {
      mocks.speechModels.finishDiscovery();
      await settled();
    }
    return mounted;
  }

  function composerBox(tree: unknown): AnyVnode {
    return findByAttr(tree, "data-part", "chat.composer")!;
  }

  /** The mic, whatever it shows. */
  function mic(tree: unknown): AnyVnode {
    return findByClass(tree, "message-input-dictate-button")!;
  }

  function micState(tree: unknown): unknown {
    return mic(tree).attrs!["data-dictation-state"];
  }

  function pressMic(render: () => unknown): void {
    (mic(render()).attrs!.onclick as () => void)();
  }

  function isLoadShown(tree: unknown): boolean {
    const isBlocked = composerBox(tree).attrs!["data-composer-state"] === "blocked-loading";
    const isTextboxDisabled = findByTag(tree, "textarea")?.attrs?.disabled === true;
    const isLabelLive = findByClass(tree, "dictation-morph-label")?.attrs?.disabled === false;
    const shown = [micState(tree) === "loading", isBlocked, isTextboxDisabled, isLabelLive];
    // The mic's label, the box's mark and the disabled text box come and go together.
    expect(new Set(shown).size, `the loading look is drawn in part: ${shown.join(", ")}`).toBe(1);
    return shown[0];
  }

  function loadState() {
    return mocks.voiceQueryClient.getQueryState(LOAD_KEY);
  }

  /** The page's last listener for ``type`` on the document: the composer mounted last. */
  function pageListener(type: string): (event: unknown) => void {
    const registered = mocks.listeners.get(type) ?? [];
    return registered[registered.length - 1];
  }

  /** The mic's chord as the platform spells it: Cmd on a Mac, Ctrl elsewhere, read off the navigator as the
   *  composer reads it. */
  function shortcutChord() {
    const isApple = isApplePlatform(navigator);
    return { key: "M", ctrlKey: !isApple, altKey: false, shiftKey: true, metaKey: isApple, preventDefault: vi.fn() };
  }

  /** Stand a text box in for the one mithril would make, and answer a redraw as mithril does: the view, then
   *  the text box's own update. */
  function withTextbox(render: () => unknown) {
    const textarea = { style: {}, scrollHeight: 28, clientHeight: 28, focus: vi.fn(), setSelectionRange: vi.fn() };
    (findByTag(render(), "textarea")!.attrs!.oncreate as (vnode: unknown) => void)({ dom: textarea });
    const redraw = (): unknown => {
      const tree = render();
      (findByTag(tree, "textarea")!.attrs!.onupdate as (vnode: unknown) => void)({ dom: textarea });
      return tree;
    };
    return { textarea, redraw };
  }

  /** Press the mic, and have the engine report the state a start reaches. */
  function startDictating(render: () => unknown, state = "listening") {
    const mic = findByAttr(render(), "aria-label", "Dictate");
    expect(mic, "the mic should be offered").toBeTruthy();
    (mic!.attrs!.onclick as () => void)();
    expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
    setDictationState(state);
  }

  function setDictationState(state: string) {
    mocks.dictation!.state = state;
    mocks.dictationEvents!.onStateChange(state);
  }

  function textareaValue(render: () => unknown): unknown {
    return findByTag(render(), "textarea")?.attrs?.value;
  }

  it("offers the mic beside Send only where the page can dictate", () => {
    mocks.isDictationSupported = false;
    expect(findByAttr(composer().render(), "aria-label", "Dictate")).toBeUndefined();
    mocks.isDictationSupported = true;
    const tree = composer().render();
    const toolbar = flatten(tree).filter((vnode) => vnode.tag === "button");
    const labels = toolbar.map((vnode) => vnode.attrs?.["aria-label"]);
    expect(labels.indexOf("Dictate")).toBe(labels.indexOf("Send message") - 1);
  });

  it("dictates with the touch device's engine where a finger is the primary pointer, else the desktop's", () => {
    /** The kinds of device a composer mounted and dictating afresh asked for its dictation's options as: it
     *  asks whenever it needs them, and always as the one kind. */
    const deviceClassesAskedAs = (): string[] => {
      mocks.dictationDeviceClasses = [];
      startDictating(composer().render);
      return [...new Set(mocks.dictationDeviceClasses)];
    };
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query === TOUCH_MEDIA_QUERY }));
    expect(deviceClassesAskedAs()).toEqual(["touch"]);
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    expect(deviceClassesAskedAs()).toEqual(["desktop"]);
    // A page with no window to ask has no pointer, and dictates as a desktop.
    vi.unstubAllGlobals();
    expect(deviceClassesAskedAs()).toEqual(["desktop"]);
  });

  it("locks the box while listening, shows the words at the caret, and saves only the final ones", () => {
    const { render } = composer();
    expect(findByTag(render(), "textarea")?.attrs?.readonly).toBe(false);
    startDictating(render);
    expect(findByTag(render(), "textarea")?.attrs?.readonly).toBe(true);
    const mic = findByAttr(render(), "aria-label", "Stop dictation");
    expect(mic?.attrs?.["aria-pressed"]).toBe("true");
    mocks.dictationEvents!.onTentativeWords("hello the");
    expect(textareaValue(render)).toBe("hello the");
    expect(localStorage.getItem(`message-text:${CHAT}`)).toBeNull();
    mocks.dictationEvents!.onFinalWords("Hello there.");
    mocks.dictationEvents!.onTentativeWords("");
    mocks.dictationEvents!.onUtteranceEnd();
    expect(textareaValue(render)).toBe("Hello there.");
    expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("Hello there.");
    // The mic stops what it started.
    (mic!.attrs!.onclick as () => void)();
    expect(mocks.dictation!.stop).toHaveBeenCalledTimes(1);
    setDictationState("idle");
    expect(findByTag(render(), "textarea")?.attrs?.readonly).toBe(false);
  });

  it("finishes the words first when Enter comes while dictating, then sends once", async () => {
    const { render } = composer();
    startDictating(render);
    mocks.dictationEvents!.onFinalWords("Ship it.");
    const onkeydown = findByTag(render(), "textarea")!.attrs!.onkeydown as (event: unknown) => void;
    onkeydown({
      key: "Enter",
      ctrlKey: false,
      altKey: false,
      shiftKey: false,
      metaKey: false,
      preventDefault: () => {},
    });
    expect(mocks.dictation!.stop).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    // Send shows the wait and stays put.
    const send = findByAttr(render(), "aria-label", "Send message");
    expect(send?.attrs?.["aria-disabled"]).toBe("true");
    setDictationState("idle");
    render();
    await flushAsync();
    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage).toHaveBeenCalledWith(CHAT, withDictationNote("Ship it."), expect.any(String));
  });

  it("drops the unfinished words on Escape and keeps the final ones", () => {
    const { render } = composer();
    startDictating(render);
    mocks.dictationEvents!.onFinalWords("Keep this.");
    mocks.dictationEvents!.onTentativeWords("but not this");
    expect(textareaValue(render)).toBe("Keep this. but not this");
    const onkeydown = findByTag(render(), "textarea")!.attrs!.onkeydown as (event: unknown) => void;
    onkeydown({
      key: "Escape",
      ctrlKey: false,
      altKey: false,
      shiftKey: false,
      metaKey: false,
      preventDefault: () => {},
    });
    expect(mocks.dictation!.cancel).toHaveBeenCalledTimes(1);
    // What the engine does on cancel.
    mocks.dictationEvents!.onTentativeWords("");
    mocks.dictationEvents!.onUtteranceEnd();
    setDictationState("idle");
    expect(textareaValue(render)).toBe("Keep this.");
    expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("Keep this.");
  });

  it("tells the user what went wrong over the composer, with Retry only where it can help", () => {
    const { render } = composer();
    startDictating(render, "idle");
    mocks.dictationEvents!.onProblem({ kind: "missing", message: "No microphone was found.", canRetry: true });
    let tree = render();
    expect(renderedText(tree)).toContain("No microphone was found.");
    const retry = findByClass(tree, "message-input-dictation-retry");
    (retry!.attrs!.onclick as () => void)();
    expect(mocks.dictation!.start).toHaveBeenCalledTimes(2);
    mocks.dictationEvents!.onProblem({ kind: "blocked", message: "The microphone is blocked.", canRetry: false });
    tree = render();
    expect(findByClass(tree, "message-input-dictation-retry")).toBeUndefined();
    (findByClass(tree, "message-input-dictation-dismiss")!.attrs!.onclick as () => void)();
    expect(renderedText(render())).not.toContain("The microphone is blocked.");
  });

  it("offers Retry and Discard for words that could not be transcribed", () => {
    const { render } = composer();
    startDictating(render, "idle");
    mocks.dictation!.isHeld = true;
    mocks.dictationEvents!.onProblem({ kind: "held", message: "Some words are held.", canRetry: true });
    const tree = render();
    (findByClass(tree, "message-input-dictation-retry")!.attrs!.onclick as () => void)();
    expect(mocks.dictation!.retryHeld).toHaveBeenCalledTimes(1);
    (findByClass(tree, "message-input-dictation-discard")!.attrs!.onclick as () => void)();
    expect(mocks.dictation!.discardHeld).toHaveBeenCalledTimes(1);
  });

  it("keeps the screen awake while a dictation runs, and lets go once it is idle and when the composer leaves", () => {
    const { render } = composer();
    const lastWanted = () => mocks.screenWakeWanted[mocks.screenWakeWanted.length - 1];
    startDictating(render, "starting");
    expect(lastWanted()).toBe(true);
    setDictationState("listening");
    expect(lastWanted()).toBe(true);
    setDictationState("idle");
    expect(lastWanted()).toBe(false);
    // A dictation still running when the composer leaves is let go of without a state change of its own.
    pressMic(render);
    setDictationState("listening");
    expect(lastWanted()).toBe(true);
    (findByClass(render(), "message-input mx-auto")!.attrs!.onremove as () => void)();
    expect(lastWanted()).toBe(false);
  });

  it("keeps the screen awake while the model's load is shown, and not once it is dismissed or done", async () => {
    const { render } = await composerWithoutModel();
    const lastWanted = () => mocks.screenWakeWanted[mocks.screenWakeWanted.length - 1];
    pressMic(render);
    expect(isLoadShown(render())).toBe(true);
    expect(lastWanted()).toBe(true);
    // A load left to run on in the background holds nothing.
    pageListener("keydown")({ key: "Escape", defaultPrevented: false, target: document, preventDefault: vi.fn() });
    expect(isLoadShown(render())).toBe(false);
    expect(lastWanted()).toBe(false);
    pressMic(render);
    expect(lastWanted()).toBe(true);
    mocks.speechModels.loads[0].resolve(LOADED);
    await settled();
    expect(micState(render())).toBe("ready");
    expect(lastWanted()).toBe(false);
  });

  it("wakes the chime at the press and rings it as listening begins", () => {
    const { render } = composer();
    startDictating(render, "starting");
    expect(mocks.readyChime.wake).toHaveBeenCalledTimes(1);
    expect(mocks.readyChime.play).not.toHaveBeenCalled();
    setDictationState("listening");
    expect(mocks.readyChime.play).toHaveBeenCalledTimes(1);
  });

  it("toggles dictation from the shortcut anywhere on the page, and lets go of it when the composer leaves", () => {
    const windowListeners = new Map<string, unknown>();
    vi.stubGlobal("window", {
      addEventListener: (type: string, fn: unknown) => windowListeners.set(type, fn),
      removeEventListener: (type: string) => windowListeners.delete(type),
    });
    try {
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const { render } = unmountedComposer();
      const root = findByClass(render(), "message-input mx-auto")!;
      (root.attrs!.oncreate as () => void)();
      const onShortcut = pageListener("keydown");
      const chord = shortcutChord();
      onShortcut(chord);
      expect(chord.preventDefault).toHaveBeenCalledTimes(1);
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
      onShortcut({
        key: "m",
        ctrlKey: false,
        altKey: false,
        shiftKey: false,
        metaKey: false,
        preventDefault: vi.fn(),
      });
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
      expect(windowListeners.has("pagehide")).toBe(true);
      const loadQuery = mocks.voiceQueryClient.getQueryCache().find({ queryKey: LOAD_KEY })!;
      const progressQuery = mocks.voiceQueryClient.getQueryCache().find({ queryKey: PROGRESS_KEY })!;
      expect([loadQuery.getObserversCount(), progressQuery.getObserversCount()]).toEqual([1, 1]);
      (root.attrs!.onremove as () => void)();
      expect(mocks.dictation!.dispose).toHaveBeenCalledTimes(1);
      expect(mocks.listeners.get("keydown")).not.toContain(onShortcut);
      expect(windowListeners.has("pagehide")).toBe(false);
      // The page's speech model is no longer followed, and stays as it was.
      expect([loadQuery.getObserversCount(), progressQuery.getObserversCount()]).toEqual([0, 0]);
      expect(loadState()).toMatchObject({ status: "success", data: LOADED });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps a held shortcut's repeats from the browser, and toggles nothing with them", () => {
    composer();
    const onShortcut = pageListener("keydown");
    const heldBeforeAnyPress = { ...shortcutChord(), repeat: true };
    onShortcut(heldBeforeAnyPress);
    expect(heldBeforeAnyPress.preventDefault).toHaveBeenCalledTimes(1);
    expect(mocks.dictation).toBeNull();
    // The press itself toggles as ever; held on, it neither stops what it started nor starts it again.
    onShortcut({ ...shortcutChord(), repeat: false });
    expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
    setDictationState("listening");
    const heldWhileListening = { ...shortcutChord(), repeat: true };
    onShortcut(heldWhileListening);
    expect(heldWhileListening.preventDefault).toHaveBeenCalledTimes(1);
    expect(mocks.dictation!.stop).not.toHaveBeenCalled();
    onShortcut(shortcutChord());
    expect(mocks.dictation!.stop).toHaveBeenCalledTimes(1);
    expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
  });

  describe("the speech model's load", () => {
    it("draws the plain mic until the page knows what state the model is in", async () => {
      const { render } = unmountedComposer();
      expect(micState(render())).toBe("ready");
      const mounted = await composerWithoutModel({ isDiscovered: false });
      expect(mocks.speechModels.discover).toHaveBeenCalledTimes(1);
      const tree = mounted.render();
      expect(micState(tree)).toBe("ready");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate");
      expect(findByClass(tree, "dictation-setup-badge")).toBeUndefined();
      mocks.speechModels.finishDiscovery();
      await settled();
      expect(micState(mounted.render())).toBe("load-needed");
    });

    it("asks for the model once the probe has failed, with no download to name", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      mocks.speechModelProbe.mockRejectedValue(new Error("no storage"));
      const { render } = unmountedComposer();
      putOnPage(render);
      expect(micState(render())).toBe("ready");
      await settled();
      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate once the speech model has loaded");
      expect(mic(tree).attrs!["data-hover-tooltip"]).toMatch(/^Dictate once the speech model has loaded \(/);
      expect(mocks.speechModels.discover).not.toHaveBeenCalled();
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("no storage"));
      warn.mockRestore();
    });

    it("asks for the model once discovery has failed", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
      mocks.speechModels.discover.mockRejectedValue(new Error("no worker"));
      const { render } = await composerWithoutModel({ isDownloaded: true, isDiscovered: false });
      expect(mocks.speechModels.discover).toHaveBeenCalledTimes(1);
      expect(micState(render())).toBe("load-needed");
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("no worker"));
      warn.mockRestore();
    });

    it("stops waiting on a worker that neither answers nor fails, five seconds from the mount", async () => {
      vi.useFakeTimers();
      const { render } = unmountedComposer();
      putOnPage(render);
      await vi.advanceTimersByTimeAsync(4999);
      expect(mocks.speechModels.discover).toHaveBeenCalledTimes(1);
      expect(micState(render())).toBe("ready");
      const redraw = vi.spyOn(m, "redraw");
      try {
        await vi.advanceTimersByTimeAsync(1);
        expect(redraw).toHaveBeenCalled();
      } finally {
        redraw.mockRestore();
      }
      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(mic(tree).attrs!["data-hover-tooltip"]).toMatch(/^Dictate once the speech model has loaded \(/);
      // The worker greeting late, with the model loaded, still turns the mic ready.
      mocks.speechModels.finishDiscovery();
      await vi.advanceTimersByTimeAsync(0);
      expect(micState(render())).toBe("load-needed");
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      expect(micState(render())).toBe("ready");
    });

    it("stops waiting on the model's state when the composer leaves the page", async () => {
      vi.useFakeTimers();
      const { render } = unmountedComposer();
      putOnPage(render);
      await vi.advanceTimersByTimeAsync(1000);
      expect(vi.getTimerCount()).toBe(1);
      (findByClass(render(), "message-input mx-auto")!.attrs!.onremove as () => void)();
      expect(vi.getTimerCount()).toBe(0);
    });

    it("asks for a model that is not loaded, naming the download a first load is", async () => {
      const { render } = await composerWithoutModel({ isDownloaded: false });
      expect(mocks.speechModels.discover).toHaveBeenCalledWith(
        expect.objectContaining({ engineId: "moonshine-base" }),
        false,
      );
      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate once the speech model has loaded");
      expect(findByClass(tree, "dictation-setup-badge")).toBeDefined();
      // The size is formatted by the mocked attachments module here; DictationButton's own tests read the real one.
      expect(mic(tree).attrs!["data-hover-tooltip"]).toMatch(/^Dictate after a one-time .+ download \(/);
      expect(isLoadShown(tree)).toBe(false);
    });

    it("asks for a model whose files are on this device as a load, not a download", async () => {
      const { render } = await composerWithoutModel({ isDownloaded: true });
      expect(mocks.speechModels.discover).toHaveBeenCalledWith(
        expect.objectContaining({ engineId: "moonshine-base" }),
        true,
      );
      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate once the speech model has loaded");
      expect(mic(tree).attrs!["data-hover-tooltip"]).toMatch(/^Dictate once the speech model has loaded \(/);
    });

    it("asks for the load once at a press and shows it: the label and the blocked box, with no wash over it", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
      expect(mocks.speechModels.request).toHaveBeenCalledWith(expect.objectContaining({ engineId: "moonshine-base" }));
      expect(mocks.speechModels.loads.length).toBe(1);
      const tree = render();
      expect(isLoadShown(tree)).toBe(true);
      expect(composerBox(tree).attrs!["data-composer-state"]).toBe("blocked-loading");
      expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
      expect(findByClass(tree, "message-input-scrim")).toBeUndefined();
      expect(findByClass(tree, "dictation-setup-badge")).toBeUndefined();
      // The buttons beside the box stay live, and nothing listens.
      expect(findByAttr(tree, "aria-label", "Attach files")?.attrs?.disabled).toBe(false);
      expect(mic(tree).attrs!.disabled).toBe(false);
      expect(mocks.dictation).toBeNull();
    });

    it("loads at a press that comes before the page knows the model's state, when the model is not loaded", async () => {
      const { render } = await composerWithoutModel({ isDiscovered: false });
      expect(micState(render())).toBe("ready");
      pressMic(render);
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
      expect(mocks.dictation).toBeNull();
      expect(isLoadShown(render())).toBe(true);
    });

    // ``repeatsThePress``: whether the trigger, coming right behind the press that asked for the load, is taken
    // for that press landing twice.
    describe.each([
      {
        trigger: "a press on the label",
        repeatsThePress: true,
        dismiss: (render: () => unknown) => {
          (findByClass(render(), "dictation-morph-label")!.attrs!.onclick as () => void)();
        },
      },
      { trigger: "a press on the mic", repeatsThePress: true, dismiss: (render: () => unknown) => pressMic(render) },
      {
        trigger: "the mic's shortcut",
        repeatsThePress: true,
        dismiss: () => pageListener("keydown")(shortcutChord()),
      },
      {
        trigger: "Escape from the page",
        repeatsThePress: false,
        dismiss: () => {
          pageListener("keydown")({
            key: "Escape",
            defaultPrevented: false,
            target: document,
            preventDefault: vi.fn(),
          });
        },
      },
      {
        trigger: "the shell saying the page is hidden",
        repeatsThePress: false,
        dismiss: () => stopDictationWhenHidden(),
      },
      {
        trigger: "the page going out of view",
        repeatsThePress: false,
        dismiss: () => {
          const page = document as unknown as { visibilityState?: string };
          page.visibilityState = "hidden";
          try {
            pageListener("visibilitychange")({});
          } finally {
            delete page.visibilityState;
          }
        },
      },
    ])("dismissed by $trigger", ({ dismiss, repeatsThePress }) => {
      it("hands the box back, leaves the mic asking for the model, and lets the load run on", async () => {
        vi.useFakeTimers({ toFake: ["Date"] });
        const { render } = await composerWithoutModel();
        const { textarea, redraw } = withTextbox(render);
        pressMic(render);
        expect(isLoadShown(redraw())).toBe(true);
        expect(textarea.focus).toHaveBeenCalledTimes(1);
        // Half a second on, no press is the first one landing twice.
        vi.advanceTimersByTime(500);

        dismiss(render);

        const tree = redraw();
        expect(isLoadShown(tree)).toBe(false);
        expect(micState(tree)).toBe("load-needed");
        expect(composerBox(tree).attrs!["data-composer-state"]).toBeUndefined();
        expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(false);
        expect(textarea.focus).toHaveBeenCalledTimes(2);
        // Neither asked for again nor called off.
        expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
        expect(mocks.speechModels.loads.length).toBe(1);
        expect(loadState()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
        expect(mocks.dictation).toBeNull();
        expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      });

      it(`${repeatsThePress ? "does nothing" : "dismisses all the same"} right behind the press that asked for the load`, async () => {
        vi.useFakeTimers({ toFake: ["Date"] });
        const { render } = await composerWithoutModel();
        pressMic(render);
        vi.advanceTimersByTime(499);

        dismiss(render);

        expect(isLoadShown(render())).toBe(repeatsThePress);
        expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
        expect(mocks.dictation).toBeNull();
      });
    });

    it("takes a second press right behind one that shows a running load again for the same press too", async () => {
      vi.useFakeTimers({ toFake: ["Date"] });
      const { render } = await composerWithoutModel();
      pressMic(render);
      vi.advanceTimersByTime(5000);
      stopDictationWhenHidden();
      expect(isLoadShown(render())).toBe(false);
      pressMic(render);
      vi.advanceTimersByTime(100);
      pressMic(render);
      expect(isLoadShown(render())).toBe(true);
      expect(mocks.speechModels.loads.length).toBe(1);
    });

    it("keeps a load shown through a held shortcut's repeats, however long it is held", async () => {
      vi.useFakeTimers({ toFake: ["Date"] });
      const { render } = await composerWithoutModel();
      const onShortcut = pageListener("keydown");
      onShortcut(shortcutChord());
      expect(isLoadShown(render())).toBe(true);
      vi.advanceTimersByTime(600);
      const held = { ...shortcutChord(), repeat: true };
      onShortcut(held);
      expect(held.preventDefault).toHaveBeenCalledTimes(1);
      expect(isLoadShown(render())).toBe(true);
      // Let go and pressed again, the shortcut dismisses.
      onShortcut(shortcutChord());
      expect(isLoadShown(render())).toBe(false);
    });

    it("dismisses on Escape from the page or the composer, and leaves a key meant for another control alone", async () => {
      const { render } = await composerWithoutModel();
      const onKeydown = pageListener("keydown");
      const escape = (target: unknown) => ({
        key: "Escape",
        defaultPrevented: false,
        target,
        preventDefault: vi.fn(),
      });
      // With no load shown the key is nobody's here.
      const atRest = escape(document);
      onKeydown(atRest);
      expect(atRest.preventDefault).not.toHaveBeenCalled();
      pressMic(render);
      // A key from some other control (not the page, not an element in the composer) is that control's.
      const fromAnotherControl = escape({ closest: () => null });
      onKeydown(fromAnotherControl);
      expect(fromAnotherControl.preventDefault).not.toHaveBeenCalled();
      expect(isLoadShown(render())).toBe(true);
      const fromThePage = escape(document);
      onKeydown(fromThePage);
      expect(fromThePage.preventDefault).toHaveBeenCalledTimes(1);
      expect(isLoadShown(render())).toBe(false);
      // Dismissed, the load is no longer the key's to answer.
      const afterwards = escape(document);
      onKeydown(afterwards);
      expect(afterwards.preventDefault).not.toHaveBeenCalled();
    });

    it("shows the label again at a press after a dismissal, on the same load and where it has got to", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      stopDictationWhenHidden();
      mocks.voiceQueryClient.setQueryData(PROGRESS_KEY, { loadedBytes: 43, totalBytes: 100 });
      expect(isLoadShown(render())).toBe(false);

      pressMic(render);

      const tree = render();
      expect(isLoadShown(tree)).toBe(true);
      // The load in flight is joined: no second one starts.
      expect(mocks.speechModels.loads.length).toBe(1);
      expect((findByClass(tree, "dictation-morph-bar")!.attrs!.style as { width: string }).width).toBe("43%");
      expect(findByClass(tree, "dictation-morph-label")!.attrs!["data-hover-tooltip"]).toMatch(
        /^Downloading speech model, 43% of .+\. Dictation starts when it is done; click to keep typing instead\.$/,
      );
      // One end of the load ends both watches of it.
      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();
      expect(isLoadShown(render())).toBe(false);
      expect(micState(render())).toBe("ready");
    });

    it("turns the mic ready in silence when a load finishes after a dismissal", async () => {
      const { render } = await composerWithoutModel();
      const { textarea, redraw } = withTextbox(render);
      pressMic(render);
      redraw();
      stopDictationWhenHidden();
      expect(micState(redraw())).toBe("load-needed");
      const focusesBefore = textarea.focus.mock.calls.length;

      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();

      const tree = redraw();
      expect(micState(tree)).toBe("ready");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate");
      expect(mocks.readyChime.play).not.toHaveBeenCalled();
      expect(mocks.dictation).toBeNull();
      expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      expect(textarea.focus).toHaveBeenCalledTimes(focusesBefore);
    });

    it("goes on to dictate when a watched load finishes, with no second press", async () => {
      const { render } = await composerWithoutModel();
      const { textarea, redraw } = withTextbox(render);
      pressMic(render);
      expect(isLoadShown(redraw())).toBe(true);
      expect(textarea.focus).toHaveBeenCalledTimes(1);

      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();

      // The one press asked for the model and for the dictation: the load's end starts it.
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
      const tree = redraw();
      expect(isLoadShown(tree)).toBe(false);
      expect(textarea.focus).toHaveBeenCalledTimes(2);
      expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      expect(mocks.readyChime.play).not.toHaveBeenCalled();
      setDictationState("starting");
      setDictationState("listening");
      expect(micState(redraw())).toBe("listening");
      expect(mocks.readyChime.play).toHaveBeenCalledTimes(1);
    });

    it("starts nothing when a watched load finishes in a composer that takes no input", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      // The chat's account is signed out while the model loads, which disables the composer.
      mocks.agent.account_id = "account-signed-out-5521";
      mocks.signedOutAccountIds.add("account-signed-out-5521");
      try {
        render();

        mocks.speechModels.loads[0].resolve(LOADED);
        await settled();

        expect(mocks.dictation).toBeNull();
        expect(micState(render())).toBe("ready");
      } finally {
        mocks.agent.account_id = null;
        mocks.signedOutAccountIds.clear();
      }
    });

    it("says why a watched load failed, and asks for the model again on Retry", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      mocks.speechModels.loads[0].reject(
        new EngineLoadError("config.json does not match its pinned SHA-256", "config.json"),
      );
      await settled();

      let tree = render();
      expect(isLoadShown(tree)).toBe(false);
      expect(micState(tree)).toBe("load-needed");
      const notice = findByClass(tree, "message-input-dictation-notice")!;
      expect(notice.attrs!["data-dictation-problem"]).toBe("download");
      expect(renderedText(notice)).toContain("The speech model couldn't be downloaded (config.json).");

      (findByClass(tree, "message-input-dictation-retry")!.attrs!.onclick as () => void)();

      tree = render();
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(2);
      expect(mocks.speechModels.loads.length).toBe(2);
      expect(isLoadShown(tree)).toBe(true);
      expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      expect(mocks.dictation).toBeNull();
    });

    it("says nothing when a load fails after a dismissal, and asks again at the next press", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      stopDictationWhenHidden();
      mocks.speechModels.loads[0].reject(new EngineLoadError("no WebGPU adapter", null));
      await settled();

      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      expect(loadState()).toMatchObject({ status: "error", fetchStatus: "idle" });

      pressMic(render);
      expect(mocks.speechModels.loads.length).toBe(2);
      expect(isLoadShown(render())).toBe(true);
    });

    it("takes a failed load's notice down once the model is seen loaded", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      mocks.speechModels.loads[0].reject(new EngineLoadError("no WebGPU adapter", null));
      await settled();
      expect(findByClass(render(), "message-input-dictation-notice")!.attrs!["data-dictation-problem"]).toBe("engine");
      // What the worker's word that another panel's load finished does to the page's cache.
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const tree = render();
      expect(micState(tree)).toBe("ready");
      expect(findByClass(tree, "message-input-dictation-notice")).toBeUndefined();
      expect(mocks.dictation).toBeNull();
    });

    it("keeps a notice that is not a failed load's when the model is seen loaded", async () => {
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const { render } = await composerWithoutModel({ isDownloaded: true });
      startDictating(render, "idle");
      mocks.dictationEvents!.onProblem({ kind: "missing", message: "No microphone was found.", canRetry: true });
      // The worker lets the model go, and a load asked for elsewhere brings it back.
      await mocks.voiceQueryClient.resetQueries({ queryKey: LOAD_KEY, exact: true });
      expect(micState(render())).toBe("load-needed");
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const tree = render();
      expect(micState(tree)).toBe("ready");
      expect(findByClass(tree, "message-input-dictation-notice")!.attrs!["data-dictation-problem"]).toBe("missing");
    });

    it("dictates at once at a press when the model is loaded, with no loading look on the way", async () => {
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const { render } = await composerWithoutModel({ isDownloaded: true });
      const seen: unknown[] = [];
      const draw = () => {
        const tree = render();
        seen.push(micState(tree));
        expect(isLoadShown(tree)).toBe(false);
      };
      draw();
      pressMic(render);
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
      draw();
      setDictationState("starting");
      draw();
      setDictationState("listening");
      draw();
      expect(seen).toEqual(["ready", "ready", "starting", "listening"]);
      expect(mocks.speechModels.request).not.toHaveBeenCalled();
    });

    it("turns the mic ready when the model comes loaded without this page asking", async () => {
      const { render } = await composerWithoutModel();
      expect(micState(render())).toBe("load-needed");
      // What the worker's word that another panel's load finished does to the page's cache.
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      const tree = render();
      expect(micState(tree)).toBe("ready");
      expect(mic(tree).attrs!["aria-label"]).toBe("Dictate");
      expect(mocks.speechModels.request).not.toHaveBeenCalled();
      expect(mocks.readyChime.play).not.toHaveBeenCalled();
    });

    it("asks for a model the worker let go as a load, its files being on this device since it loaded", async () => {
      const { render } = await composerWithoutModel({ isDownloaded: false });
      pressMic(render);
      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();
      expect(micState(render())).toBe("ready");
      await mocks.voiceQueryClient.resetQueries({ queryKey: LOAD_KEY, exact: true });
      const tree = render();
      expect(micState(tree)).toBe("load-needed");
      expect(mic(tree).attrs!["data-hover-tooltip"]).toMatch(/^Dictate once the speech model has loaded \(/);
    });

    it("comes to rest as soon as the worker says the model is loaded, ahead of the request's own answer", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      // What the worker's word does to the page's cache, a step before the load's own promise settles.
      mocks.voiceQueryClient.setQueryData(LOAD_KEY, LOADED);
      expect(loadState()).toMatchObject({ status: "success", fetchStatus: "fetching" });
      const tree = render();
      expect(isLoadShown(tree)).toBe(false);
      expect(micState(tree)).toBe("ready");
      // A press in that step dictates, as any press on a loaded model does, and the load's own answer starts
      // no second dictation.
      startDictating(render);
      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
      expect(micState(render())).toBe("listening");
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
    });

    it("shows a load only in the composer that asked for it, and only until that load settles", async () => {
      const asking = await composerWithoutModel();
      const other = await composerWithoutModel();
      pressMic(asking.render);
      expect(isLoadShown(asking.render())).toBe(true);
      expect(isLoadShown(other.render())).toBe(false);
      expect(micState(other.render())).toBe("load-needed");
      mocks.speechModels.loads[0].reject(new EngineLoadError("no WebGPU adapter", null));
      await settled();
      expect(findByClass(asking.render(), "message-input-dictation-notice")).toBeDefined();
      expect(findByClass(other.render(), "message-input-dictation-notice")).toBeUndefined();
      // The other composer's load is its own to show: the first one's watch ended with the load it was for.
      pressMic(other.render);
      expect(isLoadShown(other.render())).toBe(true);
      expect(isLoadShown(asking.render())).toBe(false);
      expect(micState(asking.render())).toBe("load-needed");
    });

    it("stops showing a load when the composer turns to another chat", async () => {
      const { component, render } = await composerWithoutModel();
      pressMic(render);
      expect(isLoadShown(render())).toBe(true);
      const elsewhere = component.view!({ attrs: { chatId: "agent-elsewhere" } } as never);
      expect(isLoadShown(elsewhere)).toBe(false);
      expect(micState(elsewhere)).toBe("load-needed");
      expect(isLoadShown(render())).toBe(false);
      expect(loadState()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
    });

    it("leaves a load running when the composer that asked for it leaves the page", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      (findByClass(render(), "message-input mx-auto")!.attrs!.onremove as () => void)();
      expect(loadState()).toMatchObject({ status: "pending", fetchStatus: "fetching" });
      // A composer mounted since finds the model loaded when the load ends.
      const mountedLater = await composerWithoutModel();
      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();
      expect(micState(mountedLater.render())).toBe("ready");
      expect(mocks.speechModels.loads.length).toBe(1);
    });

    it("redraws for the download's progress only as its whole percent changes", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      const redraw = vi.spyOn(m, "redraw");
      try {
        const report = (loadedBytes: number) =>
          mocks.voiceQueryClient.setQueryData(PROGRESS_KEY, { loadedBytes, totalBytes: 1000 });
        report(0);
        expect(redraw).toHaveBeenCalledTimes(1);
        report(4);
        report(9);
        expect(redraw).toHaveBeenCalledTimes(1);
        report(10);
        expect(redraw).toHaveBeenCalledTimes(2);
        report(19);
        expect(redraw).toHaveBeenCalledTimes(2);
        mocks.voiceQueryClient.setQueryData(PROGRESS_KEY, null);
        expect(redraw).toHaveBeenCalledTimes(3);
      } finally {
        redraw.mockRestore();
      }
      expect((findByClass(render(), "dictation-morph-bar")!.attrs!.style as { width: string }).width).toBe("0%");
    });
  });

  describe("the note a dictated message carries for the model", () => {
    const TARGET = { id: "acct-openai", harness: "codex", label: "OpenAI (Codex)" };
    const AWAITING_FIRST_SEND = {
      chat_id: CHAT,
      name: "Getting started",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: true,
      held_sends: [],
    };

    beforeEach(() => {
      mocks.switchChat.mockClear();
      mocks.launchChat.mockClear();
      mocks.chooseFastMode.mockClear();
      mocks.openProviderChooser.mockReset();
      mocks.hasUserTurnLandedSince.mockClear();
      mocks.drainToComposer.mockReset();
      mocks.drainToComposer.mockResolvedValue({ block: "" });
      mocks.agent.harness = "claude";
      mocks.agent.activity_state = undefined;
    });

    afterEach(() => {
      mocks.agent.activity_state = undefined;
      mocks.switching.target = null;
      mocks.isChatRegistered = true;
      mocks.provisional = undefined;
      mocks.selectedAccount = null;
      mocks.accountsLoaded = true;
      mocks.getComposerAttachments.mockReturnValue([]);
    });

    /** Dictate `words` into the box: the mic's press, the words landing as final, and the engine going idle. */
    function dictate(render: () => unknown, words: string): void {
      startDictating(render);
      mocks.dictationEvents!.onFinalWords(words);
      setDictationState("idle");
    }

    /** Change the box's text, as typing or deleting in it does. */
    function editBox(render: () => unknown, text: string): void {
      const oninput = findByTag(render(), "textarea")!.attrs!.oninput as (event: unknown) => void;
      oninput({ target: { value: text, style: {}, scrollHeight: 10 } });
    }

    /** Press the send button, which reads "Switch and send" while a switch is armed, and wait for the send. */
    async function pressSend(render: () => unknown, label = "Send message"): Promise<void> {
      const send = findByAttr(render(), "aria-label", label);
      expect(send, `"${label}" should be offered`).toBeTruthy();
      await (send!.attrs!.onclick as () => Promise<void>)();
    }

    /** The text the last send put on the wire. */
    function lastSentText(): string {
      const calls = mocks.sendMessage.mock.calls as unknown as [string, string][];
      return calls[calls.length - 1][1];
    }

    it("marks a draft dictation wrote into, and sends it once with the note after the words", async () => {
      const { render } = composer();
      dictate(render, "Rename the parser module.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);

      await pressSend(render);

      expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
      expect(lastSentText()).toBe(`Rename the parser module.\n\n${DICTATION_NOTE}`);
      expect(hasDictatedSpeech(CHAT)).toBe(false);

      // The next message is typed: the sent one took its note with it.
      editBox(render, "typed by hand");
      await pressSend(render);
      expect(lastSentText()).toBe("typed by hand");
    });

    it("sends a typed message as it is, with no note", async () => {
      await typeAndSend(composer().component, CHAT, "typed by hand");

      expect(lastSentText()).toBe("typed by hand");
      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it("does not mark a draft for words that never became final", async () => {
      const { render } = composer();
      startDictating(render);
      mocks.dictationEvents!.onTentativeWords("rename the");
      expect(textareaValue(render)).toBe("rename the");
      expect(hasDictatedSpeech(CHAT)).toBe(false);
      // What the engine does when the dictation is called off.
      mocks.dictationEvents!.onTentativeWords("");
      mocks.dictationEvents!.onUtteranceEnd();
      setDictationState("idle");

      editBox(render, "typed by hand");
      await pressSend(render);

      expect(lastSentText()).toBe("typed by hand");
    });

    it("keeps the note on a dictated draft that was edited and partly deleted", async () => {
      const { render } = composer();
      dictate(render, "Rename the parser module.");
      editBox(render, "Rename the");
      render();
      expect(hasDictatedSpeech(CHAT)).toBe(true);

      await pressSend(render);

      expect(lastSentText()).toBe(withDictationNote("Rename the"));
    });

    it("drops the note once the box has been emptied, whatever is typed into it next", async () => {
      const { render } = composer();
      dictate(render, "Rename the parser module.");
      editBox(render, "");
      render();
      expect(hasDictatedSpeech(CHAT)).toBe(false);

      editBox(render, "typed by hand");
      await pressSend(render);

      expect(lastSentText()).toBe("typed by hand");
    });

    it("drops the note when a command the composer acts on empties the box", async () => {
      const { render } = composer();
      dictate(render, "Fast on.");
      editBox(render, "/fast on");

      await pressSend(render);
      expect(mocks.chooseFastMode).toHaveBeenCalledTimes(1);
      expect(mocks.sendMessage).not.toHaveBeenCalled();
      expect(textareaValue(render)).toBe("");

      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it.each(["/compact", "!ls -la"])(
      "sends a dictated draft that reads as a command (%s) without the note",
      async (command) => {
        const { render } = composer();
        dictate(render, "Compact.");
        editBox(render, command);

        await pressSend(render);

        expect(lastSentText()).toBe(command);
      },
    );

    it("puts the note before the attachment references, which end the message", async () => {
      mocks.getComposerAttachments.mockReturnValue([{ status: "ready", uploaded: { path: "/code/uploads/a/p.png" } }]);
      const { render } = composer();
      dictate(render, "Look at this.");

      await pressSend(render);

      // The mocked builder appends the ready paths to the text it was given.
      expect(lastSentText()).toBe(`${withDictationNote("Look at this.")} + /code/uploads/a/p.png`);
    });

    it("keeps each chat's draft to itself", async () => {
      const { component, render } = composer();
      dictate(render, "Rename the parser module.");

      await typeAndSend(component, "agent-typed-in", "typed by hand");

      expect(mocks.sendMessage).toHaveBeenLastCalledWith("agent-typed-in", "typed by hand", expect.any(String));
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    it("keeps the mark while another page showing the chat redraws its own empty box", async () => {
      const dictating = composer();
      const other = composer();
      other.render();
      dictate(dictating.render, "Rename the parser module.");

      // Every page showing a chat redraws when anything about the chat changes; this one's box was never typed in.
      expect(textareaValue(other.render)).toBe("");

      expect(hasDictatedSpeech(CHAT)).toBe(true);
      await pressSend(dictating.render);
      expect(lastSentText()).toBe(withDictationNote("Rename the parser module."));
    });

    it("still notes a dictated draft sent from a composer mounted after the words landed", async () => {
      dictate(composer().render, "Rename the parser module.");
      const mountedLater = composer();
      expect(textareaValue(mountedLater.render)).toBe("Rename the parser module.");

      await pressSend(mountedLater.render);

      expect(lastSentText()).toBe(withDictationNote("Rename the parser module."));
    });

    it("marks the draft again when a dictated message comes back from a failed send", async () => {
      mocks.sendMessage.mockRejectedValueOnce("nope");
      const { render } = composer();
      dictate(render, "Rename the parser module.");

      await pressSend(render);
      expect(textareaValue(render)).toBe("Rename the parser module.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);

      await pressSend(render);
      expect(mocks.sendMessage).toHaveBeenCalledTimes(2);
      expect(lastSentText()).toBe(withDictationNote("Rename the parser module."));
    });

    it("repeats a failed dictated message with its note on Retry, and forgets the draft once it has landed", async () => {
      mocks.sendMessage.mockRejectedValueOnce("nope");
      const { render } = composer();
      dictate(render, "Rename the parser module.");
      await pressSend(render);

      (findButton(render(), "Retry")!.attrs!.onclick as () => void)();
      await flushAsync();

      expect(mocks.sendMessage).toHaveBeenLastCalledWith(
        CHAT,
        withDictationNote("Rename the parser module."),
        expect.any(String),
        true,
      );
      expect(textareaValue(render)).toBe("");
      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it("forgets a dictated draft in the pass that takes its delivered copy out of the box", async () => {
      mocks.sendMessage.mockRejectedValueOnce({ kind: "submission_unconfirmed", toString: () => "unconfirmed" });
      const { render } = composer();
      dictate(render, "Rename the parser module.");
      await pressSend(render);
      expect(textareaValue(render)).toBe("Rename the parser module.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);

      mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
      const landed = render();

      // The turn is looked for by everything that was sent, the note included.
      expect(mocks.hasUserTurnLandedSince).toHaveBeenLastCalledWith(
        CHAT,
        withDictationNote("Rename the parser module."),
        new Set(["turn-before-the-send"]),
      );
      expect(findByTag(landed, "textarea")?.attrs?.value).toBe("");
      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it("takes the notes out of a queue Stop hands back, and sends the draft it joins with one note", async () => {
      mocks.agent.activity_state = "THINKING";
      mocks.drainToComposer.mockResolvedValueOnce({
        block: [withDictationNote("queued one"), "queued two"].join("\n"),
      });
      const { render } = composer();
      editBox(render, "my draft");

      await (findByAttr(render(), "aria-label", "Interrupt agent")!.attrs!.onclick as () => Promise<void>)();

      expect(textareaValue(render)).toBe("queued one\nqueued two\n\nmy draft");
      expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("queued one\nqueued two\n\nmy draft");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
      await pressSend(render);
      expect(lastSentText()).toBe(withDictationNote("queued one\nqueued two\n\nmy draft"));
    });

    it("leaves a draft unmarked when the queue Stop hands back holds no note", async () => {
      mocks.agent.activity_state = "THINKING";
      mocks.drainToComposer.mockResolvedValueOnce({ block: "queued one" });
      const { render } = composer();

      await (findByAttr(render(), "aria-label", "Interrupt agent")!.attrs!.onclick as () => Promise<void>)();

      expect(textareaValue(render)).toBe("queued one");
      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it("takes the note out of a block handed back from outside the composer, once, and marks the draft", () => {
      const { render } = composer();
      prependToComposer(CHAT, withDictationNote("queued one"));

      expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("queued one");
      expect(textareaValue(render)).toBe("queued one");
      expect(hasDictatedSpeech(CHAT)).toBe(true);

      // Handed back again while it still leads the draft: recognised without its note, and not stacked.
      prependToComposer(CHAT, withDictationNote("queued one"));
      expect(textareaValue(render)).toBe("queued one");

      prependToComposer("agent-typed-in", "queued two");
      expect(hasDictatedSpeech("agent-typed-in")).toBe(false);
    });

    it("moves a dictated draft to a sibling with its note, and marks a draft that is handed back", async () => {
      const attachment = {
        localId: "a1",
        fileName: "plan.pdf",
        status: "ready",
        uploaded: { path: "/uploads/plan.pdf" },
      };
      mocks.getComposerAttachments.mockReturnValue([attachment]);
      const { render } = composer();
      dictate(render, "Start over in a new chat.");

      const taken = await takeComposerDraft(CHAT);
      mocks.getComposerAttachments.mockReturnValue([]);

      expect(taken).toEqual({
        text: "Start over in a new chat.",
        // The mocked builder appends the ready paths to the text it was given.
        finalText: `${withDictationNote("Start over in a new chat.")} + /uploads/plan.pdf`,
        attachments: [attachment],
        mayContainDictatedSpeech: true,
      });
      expect(hasDictatedSpeech(CHAT)).toBe(false);
      expect(textareaValue(render)).toBe("");

      restoreComposerDraft(CHAT, taken!);

      expect(textareaValue(render)).toBe("Start over in a new chat.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    it("carries the note on the message that switches the chat", async () => {
      mocks.switching.target = TARGET;
      const { render } = composer();
      dictate(render, "Carry on in Codex.");

      await pressSend(render, "Switch and send");

      expect(mocks.switchChat).toHaveBeenCalledTimes(1);
      const [chatId, , message] = mocks.switchChat.mock.calls[0] as unknown as unknown[];
      expect([chatId, message]).toEqual([CHAT, withDictationNote("Carry on in Codex.")]);
      expect(hasDictatedSpeech(CHAT)).toBe(false);
    });

    it("takes the note out of the queue a switch returns to the composer, and marks the draft", async () => {
      mocks.switching.target = TARGET;
      mocks.switchChat.mockResolvedValueOnce({
        kind: "handoff",
        phase: "summarizing",
        returned_block: withDictationNote("queued one"),
      });
      const { render } = composer();
      editBox(render, "Carry on in Codex");

      await pressSend(render, "Switch and send");

      expect(textareaValue(render)).toBe("queued one");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    it("marks the draft again when the switch a dictated message asked for is refused", async () => {
      mocks.switching.target = TARGET;
      mocks.switchChat.mockRejectedValueOnce(new Error("Chat already runs on account acct-openai"));
      const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
      const { render } = composer();
      dictate(render, "Carry on in Codex.");

      await pressSend(render, "Switch and send");
      error.mockRestore();

      expect(textareaValue(render)).toBe("Carry on in Codex.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    it("marks the draft again when the switch is called off while a dictated message is being prepared", async () => {
      mocks.switching.target = TARGET;
      const release = holdComposerUploads();
      const { render } = composer();
      dictate(render, "Never mind.");

      const sending = pressSend(render, "Switch and send");
      await flushAsync();
      mocks.switching.target = null;
      release();
      await sending;

      expect(mocks.switchChat).not.toHaveBeenCalled();
      expect(textareaValue(render)).toBe("Never mind.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    it("launches a seeded chat with the note on a dictated first message", async () => {
      mocks.isChatRegistered = false;
      mocks.provisional = AWAITING_FIRST_SEND;
      mocks.selectedAccount = { id: "acct-1" };
      const { render } = composer();
      dictate(render, "Let's build something.");

      await pressSend(render);

      expect(mocks.launchChat).toHaveBeenCalledWith(
        CHAT,
        "acct-1",
        withDictationNote("Let's build something."),
        "agent",
      );
    });

    it("marks the draft again when no provider is chosen for a dictated first message", async () => {
      mocks.isChatRegistered = false;
      mocks.provisional = AWAITING_FIRST_SEND;
      mocks.accountsLoaded = false;
      const { render } = composer();
      dictate(render, "Let's build something.");

      const sending = pressSend(render);
      await flushAsync();
      const intent = mocks.openProviderChooser.mock.calls[0][0] as { onDismissed: () => void };
      intent.onDismissed();
      await sending;

      expect(mocks.launchChat).not.toHaveBeenCalled();
      expect(textareaValue(render)).toBe("Let's build something.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });
  });
});
