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
    startNewChatWithDraft: vi.fn(async (..._args: unknown[]) => true),
    launchAwaitingChatAsRoom: vi.fn(async (_chatId: string) => {}),
    // What the composer registered to hear of a model pick the chat app refused.
    modelChoiceFailureListener: null as ((chatId: string, error: unknown) => void) | null,
    // What the composer registered to hear of a held send that never reached its agent.
    heldSendFailedListener: null as ((chatId: string, messageId: string, detail: string, kind: string) => void) | null,
    // The account ids that read as signed out; empty at rest.
    signedOutAccountIds: new Set<string>(),
    loadAccounts: vi.fn(async () => {}),
    cancelHandoff: vi.fn(async () => ({ returned_block: "" })),
    switching,
    drainToComposer: vi.fn(async () => ({ block: "" })),
    getComposerAttachments: vi.fn(() => [] as unknown[]),
    clearComposerAttachments: vi.fn(),
    uploadDescribedFileToComposer: vi.fn(),
    uploadFilesToComposer: vi.fn(),
    stagePastedText: vi.fn(),
    unstagePastedText: vi.fn(async (_chatId: string, _localId: string): Promise<string | null> => ""),
    // Resolved at once by default: no upload in flight. A test of a send waiting on one swaps in a
    // deferred promise.
    waitForComposerUploads: vi.fn(async (_chatId: string) => {}),
    interruptAgent: vi.fn(async () => {}),
    // Whether the agent's turn for a message has reached the transcript; none has, at rest.
    hasUserTurnLandedSince: vi.fn((_chatId: string, _text: string, _priorTurnIds: ReadonlySet<string>) => false),
    openProviderChooser: vi.fn(),
    // The chat root framing the page, null for a page no root frames (the shell's own frame, a direct visit).
    framingRoot: null as { chooseProviderForFirstSend: ReturnType<typeof vi.fn> } | null,
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
    discardListeners: [] as ((discarded: DiscardedChat) => void)[],
    launchChat: vi.fn(async (_chatId: string, _accountId: string, _message?: string, _kind?: string) => ({})),
    startChatOnAccount: vi.fn(async (_accountId: string) => {}),
    openChatPage: vi.fn(),
    // Every chat the list names, and which of them is the main chat: where an ask about a chat that cannot start goes.
    chats: [] as unknown[],
    mainChatId: null as string | null,
    chooseFastMode: vi.fn(),
    selectedAccount: null as { id: string } | null,
    // Whether this page has read the account list; until it has, no account reads as missing.
    accountsLoaded: true,
    isFirstRunProviderStepPending: false,
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
  startNewChatWithDraft: mocks.startNewChatWithDraft,
  isRefusedForNewChat: (error: unknown) =>
    (error as { response?: { kind?: unknown } } | null)?.response?.kind === "cannot_move_to_account",
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
  stagePastedText: mocks.stagePastedText,
  unstagePastedText: mocks.unstagePastedText,
  uploadDescribedFileToComposer: mocks.uploadDescribedFileToComposer,
  uploadFilesToComposer: mocks.uploadFilesToComposer,
  waitForComposerUploads: (chatId: string) => mocks.waitForComposerUploads(chatId),
}));
vi.mock("../models/attachments", () => ({
  buildMessageWithAttachments: (text: string, paths: readonly string[]) =>
    paths.length === 0 ? text : `${text} + ${paths.join(" ")}`,
  formatFileSize: () => "0 B",
}));
vi.mock("../shell", () => ({ startChatOnAccount: mocks.startChatOnAccount, openChatPage: mocks.openChatPage }));
vi.mock("../models/ModelSettings", () => ({
  addModelChoiceFailureListener: (listener: (chatId: string, error: unknown) => void) => {
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
  getChatById: (chatId: string) =>
    (mocks.chats as ChatSnapshot[]).find((chat) => chat.chat_id === chatId) ??
    (mocks.isChatRegistered
      ? { title: "Chat 1", active_agent: mocks.agent, handoff: mocks.switching.handoff }
      : undefined),
  getChats: () => mocks.chats,
  getMainChatId: () => mocks.mainChatId,
  getProvisionalChat: () => mocks.provisional,
  addHeldSendFailedListener: (listener: (chatId: string, messageId: string, detail: string, kind: string) => void) => {
    mocks.heldSendFailedListener = listener;
  },
  isLaunchInFlight: () => mocks.isLaunchInFlight,
  isRoomChat: () => mocks.isRoom,
  launchChat: (chatId: string, accountId: string, message?: string, kind?: string) =>
    mocks.launchChat(chatId, accountId, message, kind),
  whenChatRegistered: (chatId: string) => mocks.whenChatRegistered(chatId),
  refuseSendToFailedChat: (chatId: string) => mocks.refuseSendToFailedChat(chatId),
  addProvisionalChatDiscardedListener: (listener: (discarded: DiscardedChat) => void) =>
    mocks.discardListeners.push(listener),
}));
vi.mock("../models/Providers", () => ({
  openProviderChooser: mocks.openProviderChooser,
  // The seeded chats these tests launch name no account of their own, so the selected one decides.
  accountForFirstSend: () => mocks.selectedAccount,
  getSelectedAccount: () => mocks.selectedAccount,
  areAccountsLoaded: () => mocks.accountsLoaded,
  isFirstRunProviderStepPending: () => mocks.isFirstRunProviderStepPending,
  isAccountSignedOut: (accountId: string | null | undefined) =>
    !!accountId && mocks.signedOutAccountIds.has(accountId),
  loadAccounts: () => mocks.loadAccounts(),
  whenAccountsReadyToChoose: () => Promise.resolve(),
}));
vi.mock("../embedApi", () => ({ framingChatRoot: () => mocks.framingRoot }));
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

import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { jsonBlock } from "@imbue/workspace-ui/src/element_reference";
import type { StartOutcome } from "@imbue/workspace-ui/src/app_contract";
import { isApplePlatform } from "@imbue/workspace-voice/src/support";
import { EngineLoadError } from "@imbue/workspace-voice/src/workerClient";
import { ChatNotStartedError } from "../models/Chats";
import type { ChatSnapshot, DiscardedChat, ProvisionalChat } from "../models/Chats";
import { chatSnapshotFixture, handoffStateFixture } from "../models/chatSnapshotFixture";
import { toggleComposerFragment } from "../models/ComposerDraft";
import { DICTATION_NOTE, hasDictatedSpeech, withDictationNote } from "../models/dictationNote";
import { stubShellConnection } from "../shellConnectionFixture";
import { describeTapFailure } from "./composer-failures";
import type { FailureLines } from "./failure-lines";
import { setMentionsForTesting } from "../models/Mentions";
import { getOutgoingMessages } from "../models/OutgoingMessages";
import { setSwitchSending } from "../models/PendingLane";
import {
  MAX_TEXTAREA_HEIGHT_PX,
  MessageInput,
  prependToComposer,
  raiseFailureNotice,
  restoreComposerDraft,
  setComposerHint,
  stopDictationWhenHidden,
  submitSuggestion,
  takeComposerDraft,
} from "./MessageInput";
import type { MessageInputAttrs } from "./MessageInput";

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

/** A refusal as mithril rejects with it: the parsed body, its status as ``code``, and the raw body as the message. */
function refusal(status: number, body: Record<string, unknown>): Error {
  return Object.assign(new Error(JSON.stringify(body)), { code: status, response: body });
}

/** A request nothing answered, as mithril rejects with it. */
function noAnswer(): Error {
  return Object.assign(new Error("null"), { code: 0, response: null });
}

/** A sibling's notice that says ``text`` whatever failed. */
function linesOf(text: string): FailureLines {
  return { body: [text], details: null, isHelpOwed: false };
}

/** A composer with a way to turn its chat over, and how often it was asked to. */
function composerWithSourceView(): { component: m.Component<MessageInputAttrs>; turned: () => number } {
  let turns = 0;
  const component = MessageInput();
  const view = component.view!.bind(component);
  return {
    component: {
      ...component,
      view: (vnode) => view({ ...vnode, attrs: { ...vnode.attrs, onTurnOnSourceView: () => (turns += 1) } }),
    },
    turned: () => turns,
  };
}

/** Find a button by its visible label. The tooltip is not inspectable -- hoverTooltipAttrs
 *  returns lifecycle hooks rather than an attribute -- so the label is the stable handle. */
function findButton(node: unknown, label: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === "button" && renderedText(vnode).includes(label));
}

/** Neither remedy of a failure that owes help is on offer. */
function expectNoHelpOffered(node: unknown): void {
  expect(findButton(node, "Ask an agent to fix this")).toBeUndefined();
  expect(findButton(node, "Report a bug")).toBeUndefined();
}

function findByTag(node: unknown, tag: string): AnyVnode | undefined {
  return flatten(node).find((vnode) => vnode.tag === tag);
}

/** The paragraphs of the notice in the tree, in order. */
function noticeParagraphs(node: unknown): string[] {
  return flatten(node)
    .filter((vnode) => String(vnode.attrs?.className ?? "").includes("modal-message"))
    .map((vnode) => renderedText(vnode).trim());
}

/** What each "See details" in the tree holds. */
function detailsShown(node: unknown): string[] {
  return flatten(node)
    .filter((vnode) => vnode.tag === SeeDetails)
    .map((vnode) => String(vnode.attrs?.details));
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

/** The message id the ``fromEnd``-th last send carried (1 for the latest). */
function sentMessageId(fromEnd = 1): string {
  const calls = mocks.sendMessage.mock.calls as unknown as [string, string, string][];
  return calls[calls.length - fromEnd][2];
}

/** Tell the composer the chat app discarded a chat not yet an agent, with the messages it dropped with it. */
function discardProvisionalChat(discarded: DiscardedChat): void {
  for (const listener of mocks.discardListeners) listener(discarded);
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
    expect(text).toContain("Say what you want in your own words instead");
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
    expect(text).toContain("Say what you want in your own words instead");
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

describe("MessageInput Enter during an input-method composition", () => {
  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    localStorage.clear();
  });

  function typeDraft(component: m.Component<{ chatId: string | null }>, text: string): void {
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)({ target: { value: text, style: {}, scrollHeight: 10 } });
  }

  function pressEnter(component: m.Component<{ chatId: string | null }>, init: Record<string, unknown>) {
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    const preventDefault = vi.fn();
    (textarea?.attrs?.onkeydown as (event: unknown) => void)({
      key: "Enter",
      ctrlKey: false,
      altKey: false,
      shiftKey: false,
      metaKey: false,
      preventDefault,
      ...init,
    });
    return preventDefault;
  }

  it("neither sends nor takes the keystroke from the input method", async () => {
    const component = MessageInput();
    typeDraft(component, "share the wo'r'k's'pa'ce");

    const preventDefault = pressEnter(component, { isComposing: true });
    await flushAsync();

    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(preventDefault).not.toHaveBeenCalled();
  });

  it("sends the finished draft once, on the Enter that follows the composition", async () => {
    const component = MessageInput();
    typeDraft(component, "share the wo'r'k's'pa'ce");
    pressEnter(component, { isComposing: true });
    typeDraft(component, "share the workspace");

    pressEnter(component, {});
    await flushAsync();

    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage).toHaveBeenCalledWith("agent-1", "share the workspace", expect.stringMatching(/^m-/));
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

describe("MessageInput long paste", () => {
  const LONG_PASTE = "a pasted line\n".repeat(400) + "the last line";
  const PASTED_SUMMARY = "Pasted text, 401 lines";

  function readyChip(localId: string, fileName: string, summary?: string) {
    return {
      localId,
      fileName,
      isImage: false,
      status: "ready",
      summary,
      uploaded: { path: `/uploads/${localId}`, name: fileName, size: 9, isImage: false, url: `/f/${localId}` },
    };
  }

  beforeEach(() => {
    mocks.agent.activity_state = undefined;
    mocks.stagePastedText.mockClear();
    mocks.uploadFilesToComposer.mockClear();
    localStorage.clear();
  });

  afterEach(() => {
    mocks.getComposerAttachments.mockReturnValue([]);
  });

  function paste(component: m.Component<{ chatId: string | null }>, text: string, files: File[] = []) {
    const textarea = findByTag(component.view!({ attrs: { chatId: "agent-1" } } as never), "textarea");
    const preventDefault = vi.fn();
    const items = files.map((file) => ({ kind: "file", getAsFile: () => file }));
    (textarea?.attrs?.onpaste as (event: unknown) => void)({
      preventDefault,
      clipboardData: { items, getData: (type: string) => (type === "text/plain" ? text : "") },
    });
    return preventDefault;
  }

  it("stages a long paste as an attachment instead of putting it in the box", () => {
    const preventDefault = paste(MessageInput(), LONG_PASTE);
    expect(preventDefault).toHaveBeenCalled();
    expect(mocks.stagePastedText).toHaveBeenCalledWith("agent-1", LONG_PASTE);
  });

  it("leaves a short paste to the box as typed text", () => {
    const preventDefault = paste(MessageInput(), "a short note");
    expect(preventDefault).not.toHaveBeenCalled();
    expect(mocks.stagePastedText).not.toHaveBeenCalled();
  });

  it("attaches a pasted image as an image, not its text", () => {
    const image = new File(["px"], "shot.png", { type: "image/png" });
    paste(MessageInput(), LONG_PASTE, [image]);
    expect(mocks.uploadFilesToComposer).toHaveBeenCalledWith("agent-1", [image]);
    expect(mocks.stagePastedText).not.toHaveBeenCalled();
  });

  it("offers to put a pasted-text chip back as text, and only on such a chip", () => {
    mocks.getComposerAttachments.mockReturnValue([
      readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY),
      readyChip("n1", "notes.txt"),
      readyChip("f1", "pasted-text-2.txt"),
    ]);
    const tree = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(tree)).toContain(PASTED_SUMMARY);
    const putBackButtons = flatten(tree).filter((vnode) => vnode.attrs?.["aria-label"] === "Paste as text");
    expect(putBackButtons).toHaveLength(1);
  });

  it("puts the text back into the box where the caret is", async () => {
    mocks.getComposerAttachments.mockReturnValue([readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY)]);
    mocks.unstagePastedText.mockResolvedValueOnce(LONG_PASTE);
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    const oninput = findByTag(render(), "textarea")?.attrs?.oninput as (event: unknown) => void;
    oninput({ target: { value: "Summarize this: ", style: {}, scrollHeight: 10 } });
    (findByAttr(render(), "aria-label", "Paste as text")?.attrs?.onclick as () => void)();
    await flushAsync();
    expect(mocks.unstagePastedText).toHaveBeenCalledWith("agent-1", "p1");
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe(`Summarize this: ${LONG_PASTE}`);
  });

  it("puts the text back once when the button is clicked again while it is read back", async () => {
    mocks.getComposerAttachments.mockReturnValue([readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY)]);
    mocks.unstagePastedText.mockClear();
    mocks.unstagePastedText.mockResolvedValue(LONG_PASTE);
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    const putBack = findByAttr(render(), "aria-label", "Paste as text")?.attrs?.onclick as () => void;
    putBack();
    putBack();
    await flushAsync();
    expect(mocks.unstagePastedText).toHaveBeenCalledTimes(1);
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe(LONG_PASTE);
    mocks.unstagePastedText.mockReset();
  });

  it("adds the text to its own chat's saved draft when the user moved to another chat meanwhile", async () => {
    mocks.getComposerAttachments.mockReturnValue([readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY)]);
    localStorage.setItem("message-text:agent-1", "Summarize this:");
    let finishReading: (text: string) => void = () => undefined;
    mocks.unstagePastedText.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          finishReading = resolve;
        }),
    );
    const component = MessageInput();
    const render = (chatId: string) => component.view!({ attrs: { chatId } } as never);
    (findByAttr(render("agent-1"), "aria-label", "Paste as text")?.attrs?.onclick as () => void)();
    mocks.getComposerAttachments.mockReturnValue([]);
    render("agent-2");
    finishReading(LONG_PASTE);
    await flushAsync();
    expect(localStorage.getItem("message-text:agent-1")).toBe(`Summarize this:\n\n${LONG_PASTE}`);
    expect(findByTag(render("agent-2"), "textarea")?.attrs?.value).toBe("");
  });

  it("keeps the text when a hand-back is waiting for the chat the user moved away from", async () => {
    mocks.getComposerAttachments.mockReturnValue([readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY)]);
    let finishReading: (text: string) => void = () => undefined;
    mocks.unstagePastedText.mockImplementationOnce(
      () =>
        new Promise<string>((resolve) => {
          finishReading = resolve;
        }),
    );
    const component = MessageInput();
    const render = (chatId: string) => component.view!({ attrs: { chatId } } as never);
    (findByAttr(render("agent-1"), "aria-label", "Paste as text")?.attrs?.onclick as () => void)();
    mocks.getComposerAttachments.mockReturnValue([]);
    render("agent-2");
    prependToComposer("agent-1", "Handed back");
    finishReading(LONG_PASTE);
    await flushAsync();
    expect(findByTag(render("agent-1"), "textarea")?.attrs?.value).toBe(`Handed back\n\n${LONG_PASTE}`);
  });

  it("says so, and keeps the attachment, when the text cannot be put back", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.getComposerAttachments.mockReturnValue([readyChip("p1", "pasted-text-1.txt", PASTED_SUMMARY)]);
    mocks.unstagePastedText.mockRejectedValueOnce(new Error("HTTP 404"));
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    (findByAttr(render(), "aria-label", "Paste as text")?.attrs?.onclick as () => void)();
    await flushAsync();
    expect(renderedText(render())).toContain("Couldn't paste the text back");
    expect(renderedText(render())).toContain("The attachment is still there, and will be sent as it is.");
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe("");
    vi.mocked(console.warn).mockRestore();
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

  describe("deleted before it lands", () => {
    const creating: ProvisionalChat = {
      chat_id: "agent-1",
      name: "Chat 2",
      account_id: "acct-1",
      kind: "agent",
      phase: "creating",
      error: null,
      is_seeded: false,
      message: "",
      held_sends: [],
      model_pick: null,
    };

    beforeEach(() => {
      mocks.isChatRegistered = false;
      mocks.provisional = creating;
    });

    afterEach(() => {
      mocks.isChatRegistered = true;
      mocks.provisional = undefined;
    });

    /** Send ``text``, which the chat app holds, then discard the chat, whose record still holds it when ``keepsHeld``. */
    async function sendThenDiscard(text: string, keepsHeld: boolean): Promise<unknown> {
      mocks.sendMessage.mockResolvedValueOnce("held");
      const component = MessageInput();
      const sent = await typeAndSend(component, "agent-1", text);
      expect(findByTag(sent, "textarea")?.attrs?.value).toBe("");
      discardProvisionalChat({
        chat_id: "agent-1",
        held_sends: keepsHeld ? [{ message_id: sentMessageId(), text }] : [],
      });
      return component.view!({ attrs: { chatId: "agent-1" } } as never);
    }

    it("puts a message the chat app held back in the box, with the not-delivered notice", async () => {
      const earlierBubbles = getOutgoingMessages("agent-1").map((outgoing) => outgoing.id);

      const after = await sendThenDiscard("hello", true);

      expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("hello");
      expect(getOutgoingMessages("agent-1").map((outgoing) => outgoing.id)).toEqual(earlierBubbles);
      expect(findButton(after, "Retry")).toBeUndefined();
    });

    it("puts several held messages back in the order they were sent", async () => {
      mocks.sendMessage.mockResolvedValueOnce("held").mockResolvedValueOnce("held");
      const component = MessageInput();
      await typeAndSend(component, "agent-1", "first");
      await typeAndSend(component, "agent-1", "second");
      const heldSends = [
        { message_id: sentMessageId(2), text: "first" },
        { message_id: sentMessageId(), text: "second" },
      ];

      discardProvisionalChat({ chat_id: "agent-1", held_sends: heldSends });

      const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("first\n\nsecond");
    });

    it("leaves alone a message Stop already took back, which the chat no longer held", async () => {
      const after = await sendThenDiscard("hello", false);

      expect(findByClass(after, "modal-message")).toBeUndefined();
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
    });

    it("puts a held message back when the chat's discard reaches the page before the send's own answer", async () => {
      let answer: (status: string) => void = () => {};
      mocks.sendMessage.mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
      const earlierBubbles = getOutgoingMessages("agent-1").map((outgoing) => outgoing.id);
      const component = MessageInput();
      const sending = typeAndSend(component, "agent-1", "hello");
      await flushAsync();

      discardProvisionalChat({ chat_id: "agent-1", held_sends: [{ message_id: sentMessageId(), text: "hello" }] });
      answer("held");
      await sending;

      const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
      expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("hello");
      expect(getOutgoingMessages("agent-1").map((outgoing) => outgoing.id)).toEqual(earlierBubbles);
    });

    it("leaves the box alone when a send's failure comes after the chat's discard put it back", async () => {
      let refuse: (error: unknown) => void = () => {};
      mocks.sendMessage.mockImplementationOnce(() => new Promise((_resolve, reject) => (refuse = reject)));
      const component = MessageInput();
      const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
      const sending = typeAndSend(component, "agent-1", "hello");
      await flushAsync();

      discardProvisionalChat({ chat_id: "agent-1", held_sends: [{ message_id: sentMessageId(), text: "hello" }] });
      render();
      refuse("nope");
      await sending;

      const after = render();
      expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("hello");
      expect(findButton(after, "Retry")).toBeUndefined();
    });

    it("leaves a retried message where it is in the box when the chat's discard comes before the retry's answer", async () => {
      let answer: (status: string) => void = () => {};
      mocks.sendMessage
        .mockRejectedValueOnce("nope")
        .mockImplementationOnce(() => new Promise((resolve) => (answer = resolve)));
      const component = MessageInput();
      const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
      const failed = await typeAndSend(component, "agent-1", "hello");
      (findButton(failed, "Retry")!.attrs!.onclick as () => void)();
      await flushAsync();
      const textarea = findByTag(render(), "textarea");
      (textarea?.attrs?.oninput as (event: unknown) => void)({
        target: { value: "and this\n\nhello", style: {}, scrollHeight: 10 },
      });

      discardProvisionalChat({ chat_id: "agent-1", held_sends: [{ message_id: sentMessageId(), text: "hello" }] });
      answer("held");
      await flushAsync();

      const after = render();
      expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("and this\n\nhello");
    });

    it("puts a retried message back when the chat is discarded after the chat app held the retry", async () => {
      mocks.sendMessage.mockRejectedValueOnce("nope").mockResolvedValueOnce("held");
      const component = MessageInput();
      const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
      const failed = await typeAndSend(component, "agent-1", "hello");
      (findButton(failed, "Retry")!.attrs!.onclick as () => void)();
      await flushAsync();
      expect(findByTag(render(), "textarea")?.attrs?.value).toBe("");

      discardProvisionalChat({ chat_id: "agent-1", held_sends: [{ message_id: sentMessageId(), text: "hello" }] });

      const after = render();
      expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
      expect(findByTag(after, "textarea")?.attrs?.value).toBe("hello");
    });
  });

  it("returns the message to the composer with the reason when the create has already failed", async () => {
    stubShellConnection(true);
    mocks.refuseSendToFailedChat.mockImplementationOnce(() => {
      throw new ChatNotStartedError("mngr create exited with code 3");
    });

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(renderedText(findByClass(after, "modal-message"))).toContain(
      "Your message wasn't delivered. Your text is back in the box, so nothing was lost.",
    );
    expect(renderedText(findByClass(after, "modal-message"))).not.toContain("Try again");
    expect(renderedText(findByClass(after, "see-details"))).toContain("mngr create exited with code 3");
    // The chat has no agent to send to again or to restart.
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    expectNoHelpOffered(after);
    // Nothing is on offer, so the notice is acknowledged rather than cancelled.
    expect(renderedText(findByClass(after, "notice-dismiss")).trim()).toBe("OK");
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");
  });

  it("hands a held send back to the composer, its reason behind See details, when the chat app reports it never landed", async () => {
    mocks.sendMessage.mockResolvedValueOnce("held");
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "held hello");
    const messageId = (mocks.sendMessage.mock.calls[0] as unknown as [string, string, string])[2];

    mocks.heldSendFailedListener!("agent-1", messageId, "The agent didn't take the message.", "failed");

    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("Your message wasn't delivered.");
    expect(findByClass(after, "see-details")).toBeDefined();
    expect(text).toContain("The agent didn't take the message.");
    expect(localStorage.getItem("message-text:agent-1")).toContain("held hello");
    expect(getOutgoingMessages("agent-1").map((outgoing) => outgoing.content)).not.toContain(
      expect.stringContaining("held hello"),
    );
  });

  it("ignores a report of a held send this page did not send", async () => {
    const component = MessageInput();

    mocks.heldSendFailedListener!("agent-1", "someone-elses-message", "refused", "input_blocked");

    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).not.toContain("Couldn't send your message");
    expect(localStorage.getItem("message-text:agent-1")).toBeNull();
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

  it("says a launch a gateway gave up on may have started the chat, not that the chat couldn't be started", async () => {
    // The gateway's own body carries a reason, but nothing says whether the chat app took the launch.
    mocks.launchChat.mockRejectedValueOnce(refusal(502, { error: "upstream connection reset" }));

    const after = await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    const text = renderedText(findByClass(after, "modal-message"));
    expect(text).toContain("The workspace didn't answer. This chat may not have started.");
    expect(text).not.toContain("report a bug");
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

  it("puts a held first send back in the box when the chat it launched is deleted before it lands", async () => {
    mocks.sendMessage.mockResolvedValueOnce("held");
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "Plan my week");

    discardProvisionalChat({
      chat_id: "agent-1",
      held_sends: [{ message_id: sentMessageId(), text: "Plan my week" }],
    });

    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(findByClass(after, "modal-message"))).toContain("Your message wasn't delivered.");
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("Plan my week");
  });

  it("hands the first send back to the composer when the chat app reports it never reached the new agent", async () => {
    mocks.sendMessage.mockResolvedValueOnce("held");
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "Plan my month");
    const messageId = (mocks.sendMessage.mock.calls[0] as unknown as [string, string, string])[2];

    mocks.heldSendFailedListener!("agent-1", messageId, "The agent didn't take the message.", "not_ready");

    expect(renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never))).toContain(
      "Couldn't send your message",
    );
    expect(localStorage.getItem("message-text:agent-1")).toContain("Plan my month");
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
    // mockRejectedValueOnce, not mockRejectedValue -- the mock is module-wide and only cleared
    // between tests, so a persistent rejection leaks into others.
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, { detail: "The agent is in shell mode with an unsubmitted command." }),
    );
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
    raiseFailureNotice("agent-1", {
      title: "Couldn't send your message",
      describe: () => linesOf("This chat is still waking up. Try again in a moment."),
      error: null,
      kind: "not_ready",
    });
    component.view!({ attrs: { chatId: "agent-1" } } as never);
    const after = await typeAndSend(component, "agent-1", "here you go");
    mocks.getComposerAttachments.mockReturnValue([]);
    expect(renderedText(after)).toContain("notes.pdf");
    expect(renderedText(after)).not.toContain("still waking up");
  });

  it("withholds Retry when the agent is unreachable, since it cannot help", async () => {
    // The chat app already tried the start once more, and Restart is the stronger way to bring it up.
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "start timed out",
        user_facing_message: "The agent isn't running and didn't start.",
        kind: "agent_unreachable",
      }),
    );
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Restart");
    expect(text).not.toContain("Retry");
    expect(text).toContain("The agent isn't running and didn't start.");
    expect(text).toContain("Restart it to send your message");
    // mngr's reason is behind See details, not in the notice's sentence.
    expect(renderedText(findByClass(after, "modal-message"))).not.toContain("start timed out");
  });

  it("doesn't mention restarting on a notice that has no Restart button", async () => {
    raiseFailureNotice("agent-1", {
      title: "Couldn't send your message",
      describe: () => linesOf("The agent isn't running and didn't start."),
      error: null,
      kind: "agent_unreachable",
    });
    const text = renderedText(MessageInput().view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("The agent isn't running and didn't start.");
    expect(text).not.toContain("Restart");
  });

  it("shows the chat app's sentence for a refused send, whatever its kind, with the reason behind See details", async () => {
    // Only the chat app knows which case it met (a start that failed, an agent still waking up), so its sentence
    // stands as written.
    for (const [kind, sentence] of [
      ["not_ready", "This chat is still waking up. Try again in a moment."],
      ["agent_start_failed", "This chat's agent couldn't be started. Try again."],
      ["chat_gone", "This chat is no longer here -- it was deleted."],
    ]) {
      mocks.sendMessage.mockRejectedValueOnce(
        refusal(kind === "not_ready" ? 503 : 500, { detail: "mngr's own words", user_facing_message: sentence, kind }),
      );
      const after = await typeAndSend(MessageInput(), "agent-1", "hello");
      expect(renderedText(findByClass(after, "modal-message"))).toContain(sentence);
      expect(renderedText(findByClass(after, "modal-message"))).not.toContain("mngr's own words");
      expect(renderedText(findByClass(after, "see-details"))).toContain("mngr's own words");
    }
  });

  it("keeps the reason for a failure nothing classified behind See details, the only account of what went wrong", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), { code: 500, response: { detail: "the host hung up" } }),
    );
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).toContain("Your message wasn't delivered.");
    expect(renderedText(after)).toContain("the host hung up");
  });

  it("says a refused send nothing classified was not delivered, keeping the chat app's words behind See details", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), { code: 409, response: { detail: "Agent 'a' is switching" } }),
    );
    const refused = renderedText(await typeAndSend(MessageInput(), "agent-1", "hello"));
    expect(refused).toContain("Couldn't send your message");
    expect(refused).toContain("Your message wasn't delivered.");
    expect(refused).toContain("Retry");
  });

  it("keeps Retry for a blocked input, which a person can clear", async () => {
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "a dialog is open", kind: "input_blocked" }));
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
    stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "Timeout waiting for message submission evidence: pane content: ❯ hello",
        kind: "submission_unconfirmed",
      }),
    );
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Couldn't confirm your message arrived");
    expect(text).toContain("The agent may already have it, so check its terminal before you send it again.");
    expect(text).not.toContain("Couldn't send your message");
    expect(renderedText(findByClass(after, "modal-message"))).not.toContain("Timeout waiting");
    expect(text).toContain("Cancel");
    expect(findButton(after, "Send again")).toBeTruthy();
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    // Checking the terminal and sending again are the whole remedy.
    expectNoHelpOffered(after);
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");
  });

  it("sends an unconfirmed message again only when the user asks", async () => {
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
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

  it("shows a sibling's own sentence for its send failure", () => {
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      describe: () => linesOf("Your queued messages weren't sent. The agent is still starting."),
      error: null,
      kind: "not_ready",
      retry: vi.fn(async () => undefined),
    });
    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("Your queued messages weren't sent. The agent is still starting.");
  });

  it("shows the new failure when a sibling's Retry fails differently", async () => {
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      describe: describeTapFailure,
      error: refusal(500, { detail: "first reason", kind: "not_ready" }),
      kind: "not_ready",
      retry: vi.fn(async () => Promise.reject(refusal(500, { detail: "the host hung up", kind: "unknown" }))),
    });
    const shown = component.view!({ attrs: { chatId: "agent-1" } } as never);
    (findButton(shown, "Try again")!.attrs!.onclick as () => void)();
    await flushAsync();
    const text = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(text).toContain("the host hung up");
    expect(text).not.toContain("first reason");
  });

  it("words a sibling's unconfirmed send the same way", () => {
    const retry = vi.fn(async () => undefined);
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      describe: () => linesOf("unconfirmed"),
      error: null,
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
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
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

  it("says a send the chat app never answered may not have been sent, offering only Send again, which sends it again under the same id", async () => {
    // The chat app may have delivered it before its answer was lost: Restart could cut that turn off and send it
    // twice, while Send again repeats the id the chat app answers once.
    stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(noAnswer());
    const { component } = composerWithSourceView();
    const after = await typeAndSend(component, "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Your message may not have been sent");
    expect(text).toContain(
      "The workspace didn't answer. Your message may not have been delivered. Your text is back in the box.",
    );
    expect(text).not.toContain("Couldn't confirm your message arrived");
    expect(findButton(after, "Send again")).toBeTruthy();
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    expect(findButton(after, "Turn on Source view")).toBeUndefined();
    expectNoHelpOffered(after);
    expect(localStorage.getItem("message-text:agent-1")).toContain("hello");

    (findButton(after, "Send again")!.attrs!.onclick as () => void)();
    await flushAsync();
    const [, , firstId] = mocks.sendMessage.mock.calls[0] as unknown as [string, string, string];
    expect(mocks.sendMessage).toHaveBeenLastCalledWith("agent-1", "hello", firstId, true);
  });

  it("takes a send nothing answered out of the composer and closes its notice once its turn lands", async () => {
    mocks.sendMessage.mockRejectedValueOnce(noAnswer());
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    expect(renderedText(after)).toContain("Your message may not have been sent");

    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const landed = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(landed)).not.toContain("Your message may not have been sent");
    expect(findByTag(landed, "textarea")?.attrs?.value).toBe("");
  });

  it("treats a gateway's reasonless 503 as a send nothing answered, not one that was refused", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("Service Unavailable"), { code: 503, response: null }),
    );
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Your message may not have been sent");
    expect(text).not.toContain("Couldn't send your message");
    expect(findButton(after, "Send again")).toBeTruthy();
    expect(findButton(after, "Retry")).toBeUndefined();
  });

  it("words a gateway's 502 that carries only its own reason as a send nothing answered, which owes no help", async () => {
    stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(refusal(502, { error: "upstream connection reset" }));
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    const text = renderedText(after);
    expect(text).toContain("Your message may not have been sent");
    expect(text).toContain("The workspace didn't answer. Your message may not have been delivered.");
    expect(text).not.toContain("Your message wasn't delivered");
    expect(findButton(after, "Send again")).toBeTruthy();
    expectNoHelpOffered(after);
  });

  it("still takes an unconfirmed message out of the composer when its turn lands after Cancel", async () => {
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
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
      throw refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" });
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
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
    await typeAndSend(component, "agent-1", "hello");
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
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
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
    mocks.hasUserTurnLandedSince.mockReturnValueOnce(true);
    const after = await typeAndSend(MessageInput(), "agent-1", "hello");
    expect(renderedText(after)).not.toContain("Couldn't confirm your message arrived");
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
  });

  it("leaves any other failed send's copy in the composer, whatever reaches the transcript", async () => {
    // Only an unconfirmed send is watched for its turn.
    mocks.hasUserTurnLandedSince.mockClear();
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "a dialog is open", kind: "input_blocked" }));
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
      mocks.sendMessage.mockRejectedValueOnce(
        Object.assign(new Error("{}"), {
          code: 409,
          response: { detail, user_facing_message: detail, kind, routine: true },
        }),
      );
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
    const sentence = "This chat is switching to Claude 2; try again once it has.";
    mocks.modelChoiceFailureListener!(
      "agent-1",
      Object.assign(new Error("[object Object]"), {
        code: 409,
        response: { detail: sentence, user_facing_message: sentence, routine: true, phase: "summarizing" },
      }),
    );

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

  it("does not send when the restart itself fails, and shows none of the send's details under it", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), { code: 500, response: { detail: "the send's own reason" } }),
    );
    const component = MessageInput();
    const after = await typeAndSend(component, "agent-1", "hello");
    expect(renderedText(after)).toContain("the send's own reason");
    mocks.sendMessage.mockClear();
    mocks.interruptAgent.mockRejectedValueOnce("restart refused");

    const restart = findButton(after, "Restart");
    (restart!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    const shown = renderedText(component.view!({ attrs: { chatId: "agent-1" } } as never));
    expect(shown).toContain("restart refused");
    expect(shown).not.toContain("the send's own reason");
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

  it("says a switch a gateway gave up on may have moved the chat, not that it is still where it was", async () => {
    mocks.switching.target = TARGET;
    mocks.switchChat.mockRejectedValueOnce(refusal(502, { error: "upstream connection reset" }));
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();

    const text = renderedText(
      findByClass(component.view!({ attrs: { chatId: "agent-1" } } as never), "modal-message"),
    );
    expect(text).toContain("The workspace didn't answer. This chat may not have moved to");
    expect(text).not.toContain("still running where it was");
  });

  it("offers a new chat on the target, not another try, when the chat can't be moved to that account", async () => {
    mocks.switching.target = TARGET;
    mocks.switchChat.mockRejectedValueOnce(
      refusal(400, {
        detail: "Chat 'agent-1' cannot change to account acct-openai in place; it is on another harness or lane",
        user_facing_message: "This chat can't be moved to that account. Start a new chat on it instead.",
        kind: "cannot_move_to_account",
        routine: true,
      }),
    );
    const component = MessageInput();
    press(findByAttr(typeDraft(component, "agent-1", "Carry on in Codex"), "aria-label", "Switch and send"));
    await flushAsync();

    const after = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(after)).toContain("This chat can't be moved to that account.");
    expect(findButton(after, "Try again")).toBeUndefined();
    (findButton(after, "Start a new chat")!.attrs!.onclick as () => void)();
    expect(mocks.startNewChatWithDraft).toHaveBeenCalledWith("agent-1", "acct-openai", null);
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

  it("offers Stop for the successor's turn once the handoff has landed, with the ordinary placeholder", () => {
    mocks.agent.activity_state = "THINKING";
    mocks.switching.handoff = handoffStateFixture({ phase: "switching", has_landed: true });
    const rendered = MessageInput().view!({ attrs: { chatId: "agent-1" } } as never);
    expect(findByAttr(rendered, "aria-label", "Interrupt agent")).toBeDefined();
    expect(findByAttr(rendered, "aria-label", "Cancel switch")).toBeUndefined();
    expect(findByTag(rendered, "textarea")?.attrs?.placeholder).toBe("Type to queue more messages...");
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

describe("a draft changed from outside the composer", () => {
  const CHAT = "agent-picking";

  beforeEach(() => {
    localStorage.clear();
  });

  it("is taken up on the composer's next pass, over what the composer held", () => {
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: CHAT } } as never);
    const textarea = findByTag(render(), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)({
      target: { value: "dog training", style: {}, scrollHeight: 10 },
    });

    toggleComposerFragment(CHAT, "Money");

    expect(findByTag(render(), "textarea")?.attrs?.value).toBe("dog training, Money, ");
    toggleComposerFragment(CHAT, "Money");
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe("dog training, ");
  });
});

describe("submitSuggestion", () => {
  const CHAT = "agent-suggested";

  beforeEach(() => {
    mocks.sendMessage.mockClear();
    mocks.launchChat.mockClear();
    mocks.switchChat.mockClear();
    mocks.openProviderChooser.mockReset();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
  });

  afterEach(() => {
    mocks.isChatRegistered = true;
    mocks.provisional = undefined;
    mocks.selectedAccount = null;
    mocks.switching.target = null;
    mocks.getComposerAttachments.mockReturnValue([]);
    setSwitchSending(CHAT, false);
  });

  const render = (component: m.Component<{ chatId: string | null }>) =>
    component.view!({ attrs: { chatId: CHAT } } as never);

  function typeDraft(component: m.Component<{ chatId: string | null }>, text: string): void {
    const textarea = findByTag(render(component), "textarea");
    (textarea?.attrs?.oninput as (event: unknown) => void)({ target: { value: text, style: {}, scrollHeight: 10 } });
  }

  /** Pick a suggestion and let the composer take it in on its next pass, as the redraw the pick asks for does. */
  async function pick(component: m.Component<{ chatId: string | null }>, label: string): Promise<unknown> {
    submitSuggestion(CHAT, label);
    render(component);
    await flushAsync();
    return render(component);
  }

  it("sends the label alone from an empty composer, and leaves the composer empty", async () => {
    const component = MessageInput();
    const after = await pick(component, "See what Studio can do");

    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage).toHaveBeenCalledWith(CHAT, "See what Studio can do", expect.stringMatching(/^m-/));
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("");
    expect(localStorage.getItem(`message-text:${CHAT}`)).toBeNull();
  });

  it("sends the draft, a newline, and the label when something is typed, with what is attached", async () => {
    const component = MessageInput();
    typeDraft(component, "It is for my bakery");
    mocks.getComposerAttachments.mockReturnValue([
      { localId: "a1", fileName: "menu.pdf", status: "ready", uploaded: { path: "/uploads/menu.pdf" } },
    ]);

    await pick(component, "Describe what you want to make");

    expect(mocks.sendMessage).toHaveBeenCalledWith(
      CHAT,
      "It is for my bakery\nDescribe what you want to make + /uploads/menu.pdf",
      expect.stringMatching(/^m-/),
    );
    expect(mocks.clearComposerAttachments).toHaveBeenCalledWith(CHAT);
  });

  it("sends each pick once: a later pass of the composer sends nothing more", async () => {
    const component = MessageInput();
    await pick(component, "More ideas");
    render(component);
    await flushAsync();

    expect(mocks.sendMessage).toHaveBeenCalledTimes(1);
  });

  it("launches a seeded chat awaiting its first send with the label as its first message", async () => {
    mocks.isChatRegistered = false;
    mocks.provisional = {
      chat_id: CHAT,
      name: "Getting started",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: true,
      held_sends: [],
    };
    mocks.selectedAccount = { id: "acct-1" };

    await pick(MessageInput(), "See what Studio can do");

    expect(mocks.launchChat).toHaveBeenCalledWith(CHAT, "acct-1", "See what Studio can do", "agent");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("carries an armed switch out with the label as its message", async () => {
    mocks.switching.target = { id: "acct-openai", harness: "codex", label: "OpenAI (Codex)" };

    await pick(MessageInput(), "Go with your recommendations");

    expect(mocks.switchChat).toHaveBeenCalledWith(
      CHAT,
      "acct-openai",
      "Go with your recommendations",
      expect.stringMatching(/^m-/),
      null,
    );
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("opens the provider chooser instead, keeping the draft, in a chat with no provider to start on", async () => {
    mocks.isChatRegistered = false;
    mocks.provisional = {
      chat_id: CHAT,
      name: "Welcome",
      account_id: "",
      phase: "awaiting_first_send",
      error: null,
      is_seeded: true,
      held_sends: [],
    };
    mocks.selectedAccount = null;
    localStorage.setItem(`message-text:${CHAT}`, "half a thought");

    const after = await pick(MessageInput(), "See what Studio can do");

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    expect(mocks.launchChat).not.toHaveBeenCalled();
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("half a thought");
    expect(localStorage.getItem(`message-text:${CHAT}`)).toBe("half a thought");
  });

  it("sends nothing and keeps the draft while a switch request is out", async () => {
    const component = MessageInput();
    typeDraft(component, "half a thought");
    setSwitchSending(CHAT, true);

    const after = await pick(component, "More ideas");

    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(mocks.switchChat).not.toHaveBeenCalled();
    expect(findByTag(after, "textarea")?.attrs?.value).toBe("half a thought");
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

  it("leaves the chooser shut as the notice appears while the first run's provider page is owed, its button still opening it", () => {
    mocks.isFirstRunProviderStepPending = true;
    try {
      mocks.openProviderChooser.mockClear();
      const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

      expect(findByClass(tree, "message-input-no-provider")!.attrs!.oncreate).toBeUndefined();
      (findButton(tree, "Choose a provider")!.attrs!.onclick as () => void)();
      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    } finally {
      mocks.isFirstRunProviderStepPending = false;
    }
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

  describe("in a chat root's frame", () => {
    beforeEach(() => {
      mocks.framingRoot = { chooseProviderForFirstSend: vi.fn() };
    });

    afterEach(() => {
      mocks.framingRoot = null;
    });

    it("leaves opening the chooser to the root as the notice appears", () => {
      const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

      const notice = findByClass(tree, "message-input-no-provider");
      expect(renderedText(tree)).toContain("Connect an AI provider to start chatting.");
      expect(notice!.attrs!.oncreate).toBeUndefined();
      expect(mocks.framingRoot!.chooseProviderForFirstSend).not.toHaveBeenCalled();
      expect(mocks.openProviderChooser).not.toHaveBeenCalled();
    });

    it("asks the root for its chooser from the notice's button, opening none of its own", () => {
      const tree = MessageInput().view!({ attrs: { chatId: "agent-first-7731" } } as never);

      (findButton(tree, "Choose a provider")!.attrs!.onclick as () => void)();

      expect(mocks.framingRoot!.chooseProviderForFirstSend).toHaveBeenCalledTimes(1);
      expect(mocks.openProviderChooser).not.toHaveBeenCalled();
    });
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
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), {
        code: 409,
        response: { detail: "You signed out of the account this chat runs on.", kind: "account_signed_out" },
      }),
    );
    const component = MessageInput();

    await typeAndSend(component, "agent-signed-out", "are you still there?");

    await vi.waitFor(() => expect(mocks.loadAccounts).toHaveBeenCalledTimes(1));
    const tree = component.view!({ attrs: { chatId: "agent-signed-out" } } as never);
    expect(findButton(tree, "Choose a provider")).toBeDefined();
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(true);
  });

  it("offers choosing a provider instead of Retry and Restart when a send is refused for a signed-out account", async () => {
    mocks.signedOutAccountIds.clear();
    const detail = "You signed out of the account this chat runs on. Choose a provider to continue it on.";
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), {
        code: 409,
        response: { detail, kind: "account_signed_out", user_facing_message: detail, routine: true },
      }),
    );
    const component = MessageInput();

    const after = await typeAndSend(component, "agent-signed-out", "are you still there?");

    const text = renderedText(after);
    expect(text).toContain("Couldn't send your message");
    expect(text).toContain("You signed out of the account this chat runs on.");
    // See details holds the status alone: the chat app's reason only repeats its sentence.
    expect(findByClass(after, "see-details")).toBeDefined();
    expect(text).toContain("HTTP 409");
    expect(text.split(detail)).toHaveLength(2);
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
    mocks.sendMessage.mockRejectedValueOnce(
      Object.assign(new Error("{}"), { code: 500, response: { detail: "a dialog is open", kind: "input_blocked" } }),
    );

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
    const isLabelLive = findByClass(tree, "dictation-morph-label")?.attrs?.disabled === false;
    const shown = [micState(tree) === "loading", isLabelLive];
    // The mic's state and its live label come and go together.
    expect(new Set(shown).size, `the loading look is drawn in part: ${shown.join(", ")}`).toBe(1);
    // The load is the mic's to show: the box around it is never marked, washed or disabled for it.
    expect(composerBox(tree).attrs!["data-composer-state"]).toBeUndefined();
    expect(findByClass(tree, "message-input-scrim")).toBeUndefined();
    expect(findByTag(tree, "textarea")?.attrs?.disabled).toBe(false);
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

    it("asks for the load once at a press and shows it on the mic alone, the box left as it is", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      expect(mocks.speechModels.request).toHaveBeenCalledTimes(1);
      expect(mocks.speechModels.request).toHaveBeenCalledWith(expect.objectContaining({ engineId: "moonshine-base" }));
      expect(mocks.speechModels.loads.length).toBe(1);
      const tree = render();
      expect(isLoadShown(tree)).toBe(true);
      expect(findByTag(tree, "textarea")?.attrs?.readonly).toBe(false);
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
        focusesTheBox: true,
        dismiss: (render: () => unknown) => {
          (findByClass(render(), "dictation-morph-label")!.attrs!.onclick as () => void)();
        },
      },
      {
        trigger: "a press on the mic",
        repeatsThePress: true,
        focusesTheBox: true,
        dismiss: (render: () => unknown) => pressMic(render),
      },
      {
        trigger: "the mic's shortcut",
        repeatsThePress: true,
        focusesTheBox: true,
        dismiss: () => pageListener("keydown")(shortcutChord()),
      },
      {
        trigger: "Escape from the page",
        repeatsThePress: false,
        focusesTheBox: true,
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
        trigger: "text entered into the box",
        repeatsThePress: false,
        focusesTheBox: false,
        dismiss: (render: () => unknown) => {
          (findByTag(render(), "textarea")!.attrs!.oninput as (event: unknown) => void)({
            target: { value: "never mind, I'll type", style: {}, scrollHeight: 10 },
          });
        },
      },
      {
        trigger: "the shell saying the page is hidden",
        repeatsThePress: false,
        focusesTheBox: false,
        dismiss: () => stopDictationWhenHidden(),
      },
      {
        trigger: "the page going out of view",
        repeatsThePress: false,
        focusesTheBox: false,
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
    ])("dismissed by $trigger", ({ dismiss, repeatsThePress, focusesTheBox }) => {
      it("stops showing the load, leaves the mic asking for the model, and lets the load run on", async () => {
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
        // A dismissal made on the load itself is the user turning back to typing.
        expect(textarea.focus).toHaveBeenCalledTimes(focusesTheBox ? 2 : 1);
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

    it("keeps the load shown at a press in the text box, and dictates there when the load ends", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      const textbox = findByTag(render(), "textarea")!;
      expect(textbox.attrs!.disabled).toBe(false);

      (textbox.attrs!.onclick as (event: unknown) => void)({
        target: { value: "", selectionStart: 0, selectionEnd: 0 },
      });

      expect(isLoadShown(render())).toBe(true);
      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();
      expect(mocks.dictation!.start).toHaveBeenCalledTimes(1);
    });

    it("starts nothing when the load ends after text was entered into the box", async () => {
      const { render } = await composerWithoutModel();
      pressMic(render);
      (findByTag(render(), "textarea")!.attrs!.oninput as (event: unknown) => void)({
        target: { value: "a", style: {}, scrollHeight: 10 },
      });
      expect(isLoadShown(render())).toBe(false);
      expect(loadState()).toMatchObject({ status: "pending", fetchStatus: "fetching" });

      mocks.speechModels.loads[0].resolve(LOADED);
      await settled();

      expect(mocks.dictation).toBeNull();
      expect(micState(render())).toBe("ready");
      expect(findByTag(render(), "textarea")?.attrs?.value).toBe("a");
    });

    it("leaves a touch device's focus where it is when the label dismisses, so no keyboard comes up", async () => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.stubGlobal("matchMedia", (query: string) => ({ matches: query === TOUCH_MEDIA_QUERY }));
      const { render } = await composerWithoutModel();
      const { textarea, redraw } = withTextbox(render);
      pressMic(render);
      vi.advanceTimersByTime(500);
      const focusesBefore = textarea.focus.mock.calls.length;

      (findByClass(render(), "dictation-morph-label")!.attrs!.onclick as () => void)();

      expect(isLoadShown(redraw())).toBe(false);
      expect(textarea.focus).toHaveBeenCalledTimes(focusesBefore);
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
      expect(textarea.focus).toHaveBeenCalledTimes(1);
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
      mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "unconfirmed", kind: "submission_unconfirmed" }));
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

    it("marks the draft again when the launch a dictated first message asked for is refused", async () => {
      mocks.isChatRegistered = false;
      mocks.provisional = AWAITING_FIRST_SEND;
      mocks.selectedAccount = { id: "acct-1" };
      mocks.launchChat.mockRejectedValueOnce(refusal(500, { detail: "mngr could not read the template" }));
      const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
      const { render } = composer();
      dictate(render, "Let's build something.");

      await pressSend(render);
      error.mockRestore();

      expect(textareaValue(render)).toBe("Let's build something.");
      expect(hasDictatedSpeech(CHAT)).toBe(true);
    });

    describe("a message held for a chat deleted before it started", () => {
      beforeEach(() => {
        mocks.isChatRegistered = false;
        mocks.provisional = { ...AWAITING_FIRST_SEND, account_id: "acct-1", phase: "creating", is_seeded: false };
        mocks.sendMessage.mockResolvedValueOnce("held");
      });

      /** Discard the chat while the chat app still holds the last message sent to it, as it went on the wire. */
      function discardHoldingLastSend(): void {
        discardProvisionalChat({ chat_id: CHAT, held_sends: [{ message_id: sentMessageId(), text: lastSentText() }] });
      }

      it("comes back without its note, and marks the draft again", async () => {
        const { render } = composer();
        dictate(render, "Rename the parser module.");
        await pressSend(render);
        expect(hasDictatedSpeech(CHAT)).toBe(false);

        discardHoldingLastSend();

        expect(textareaValue(render)).toBe("Rename the parser module.");
        expect(hasDictatedSpeech(CHAT)).toBe(true);
      });

      it("leaves the draft unmarked when it was typed", async () => {
        const { render } = composer();
        editBox(render, "typed by hand");
        await pressSend(render);

        discardHoldingLastSend();

        expect(textareaValue(render)).toBe("typed by hand");
        expect(hasDictatedSpeech(CHAT)).toBe(false);
      });
    });
  });
});

describe("MessageInput tells each way a send can fail", () => {
  beforeEach(() => {
    mocks.sendMessage.mockReset();
    mocks.sendMessage.mockResolvedValue(null);
    mocks.interruptAgent.mockClear();
    mocks.drainToComposer.mockReset();
    mocks.drainToComposer.mockResolvedValue({ block: "" });
    mocks.startChatOnAccount.mockClear();
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    mocks.selectedAccount = null;
    localStorage.clear();
  });

  it("says what a blocked input means in the chat app's words, with the agent's words behind See details", async () => {
    const sentence = "The agent is waiting on something in its terminal. Answer it there and try again.";
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "The agent is in shell mode with an unsubmitted command.",
        user_facing_message: sentence,
        kind: "input_blocked",
        routine: true,
      }),
    );
    stubShellConnection(true);
    const { component, turned } = composerWithSourceView();

    const after = await typeAndSend(component, "agent-1", "hello");
    // Answering the dialog is the way forward, so the chat app calls the refusal routine.
    expectNoHelpOffered(after);

    expect(renderedText(findByClass(after, "modal-message"))).toContain(sentence);
    expect(renderedText(findByClass(after, "see-details"))).toContain("shell mode with an unsubmitted command");
    expect(findButton(after, "Retry")).toBeDefined();
    expect(findButton(after, "Restart")).toBeDefined();
    (findButton(after, "Turn on Source view")!.attrs!.onclick as () => void)();
    expect(turned()).toBe(1);
  });

  it("offers Retry and, with no running chat to ask, Report a bug for an agent whose start failed, and no Restart", async () => {
    stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(500, {
        detail: "Could not start agent 'Chat-1' for a send: mngr start exited 1",
        user_facing_message: "This chat's agent couldn't be started. Try again.",
        kind: "agent_start_failed",
      }),
    );
    const { component } = composerWithSourceView();

    const after = await typeAndSend(component, "agent-1", "hello");

    expect(renderedText(findByClass(after, "modal-message")).trim()).toBe(
      "This chat's agent couldn't be started. Try again.",
    );
    expect(findButton(after, "Retry")).toBeDefined();
    // A new chat, which an ask through the shell may make, would meet what stopped this one.
    expect(findButton(after, "Ask an agent to fix this")).toBeUndefined();
    expect(findButton(after, "Report a bug")).toBeDefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    expect(findButton(after, "Turn on Source view")).toBeUndefined();
  });

  it("offers only Retry for a refusal whose kind the composer does not know", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(503, {
        detail: "The chat app has not read its agent list from mngr yet; try again shortly.",
        user_facing_message: "This workspace is still starting up. Try again in a few seconds.",
        kind: "chat_starting",
        routine: true,
      }),
    );
    stubShellConnection(true);
    const { component } = composerWithSourceView();

    const after = await typeAndSend(component, "agent-1", "hello");

    expect(renderedText(findByClass(after, "modal-message"))).toContain("This workspace is still starting up.");
    expect(findButton(after, "Retry")).toBeDefined();
    expectNoHelpOffered(after);
    expect(findButton(after, "Restart")).toBeUndefined();
    expect(findButton(after, "Turn on Source view")).toBeUndefined();
  });

  it("offers Turn on Source view only for a refusal the agent's terminal shows the cause of", async () => {
    for (const kind of ["not_ready", "unknown", "state_unknown"]) {
      mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "mngr's own words", kind }));
      const { component } = composerWithSourceView();

      const after = await typeAndSend(component, "agent-1", "hello");

      expect(findButton(after, "Turn on Source view"), kind).toBeUndefined();
    }
  });

  it("says to try again after a refusal with no reason, and offers an agent for it in place of Report a bug", async () => {
    stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { oops: true }));

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(renderedText(after)).toContain("Your message wasn't delivered. Your text is back in the box. Try again.");
    expect(renderedText(findByClass(after, "see-details"))).toContain("HTTP 500");
    expect(findButton(after, "Ask an agent to fix this")).toBeDefined();
    expect(findButton(after, "Report a bug")).toBeUndefined();
  });

  it("offers an agent after Retry, asking as Imbue Studio, and closes once the agent's chat opened", async () => {
    const connection = stubShellConnection(true);
    let answer: (outcome: StartOutcome) => void = () => undefined;
    vi.mocked(connection.askAgent).mockReturnValueOnce(
      new Promise<StartOutcome>((resolve) => {
        answer = resolve;
      }),
    );
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { oops: true }));
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);

    const after = await typeAndSend(component, "agent-1", "hello");
    const labels = flatten(after)
      .filter((vnode) => vnode.tag === "button")
      .map((vnode) => renderedText(vnode).trim());
    expect(labels.indexOf("Retry")).toBeGreaterThanOrEqual(0);
    expect(labels.indexOf("Ask an agent to fix this")).toBeGreaterThan(labels.indexOf("Retry"));

    (findButton(after, "Ask an agent to fix this")!.attrs!.onclick as () => void)();
    const [text, options] = vi.mocked(connection.askAgent).mock.calls[0]!;
    expect(text).toMatch(
      /^Imbue Studio showed the user this failure: Your message wasn't delivered\. Your text is back in the box\. Try again\.\n\n/,
    );
    expect(text).toContain('HTTP 500\n{"oops":true}');
    expect(options).toEqual({ showsOutcome: true });
    const asking = render();
    expect(findButton(asking, "Asking an agent…")).toBeDefined();
    expect(findButton(asking, "Retry")!.attrs!["aria-disabled"]).toBe("true");

    answer({ isOpened: true });
    await flushAsync();
    expect(renderedText(render())).not.toContain("Your message wasn't delivered");
  });

  it("tells a refused ask in the notice, then offers a draft of it and one bug report that files both", async () => {
    const connection = stubShellConnection(true);
    const refused = { message: "Chat couldn't open that.", details: "HTTP 503", isHelpOwed: true };
    vi.mocked(connection.askAgent).mockResolvedValueOnce({ isOpened: false, failure: refused });
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { oops: true }));
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);

    const after = await typeAndSend(component, "agent-1", "hello");
    (findButton(after, "Ask an agent to fix this")!.attrs!.onclick as () => void)();
    await flushAsync();

    const told = render();
    expect(renderedText(told)).toContain("Chat couldn't open that.");
    expect(findButton(told, "Ask an agent to fix this")).toBeDefined();
    (findButton(told, "Report a bug")!.attrs!.onclick as () => void)();
    expect(connection.reportBug).toHaveBeenCalledExactlyOnceWith({
      message: "Your message wasn't delivered. Your text is back in the box. Try again. Chat couldn't open that.",
      details: 'HTTP 500\n{"oops":true}\n\nHTTP 503',
    });

    // The draft leaves the same request unsent in a chat's message box, and a draft that was made closes the notice.
    vi.mocked(connection.askAgent).mockResolvedValueOnce({ isOpened: true });
    (findButton(told, "Draft it in a chat")!.attrs!.onclick as () => void)();
    const [asked, drafted] = vi.mocked(connection.askAgent).mock.calls;
    expect(drafted).toEqual([asked![0], { showsOutcome: true }, "draft"]);
    await flushAsync();
    expect(renderedText(render())).not.toContain("Your message wasn't delivered");
  });

  it("offers only to ask again after an ask refused for a reason of the person's own to clear", async () => {
    const connection = stubShellConnection(true);
    const refused = { message: "Stop viewing first.", details: null, isHelpOwed: false };
    vi.mocked(connection.askAgent).mockResolvedValueOnce({ isOpened: false, failure: refused });
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { oops: true }));
    const component = MessageInput();

    const after = await typeAndSend(component, "agent-1", "hello");
    (findButton(after, "Ask an agent to fix this")!.attrs!.onclick as () => void)();
    await flushAsync();

    const told = component.view!({ attrs: { chatId: "agent-1" } } as never);
    expect(renderedText(told)).toContain("Stop viewing first.");
    expect(findButton(told, "Ask an agent to fix this")).toBeDefined();
    expect(findButton(told, "Draft it in a chat")).toBeUndefined();
    expect(findButton(told, "Report a bug")).toBeUndefined();
  });

  it("offers Report a bug, and no agent, for a send refused because the chat app can't read its agent list, which an ask would meet too", async () => {
    const sentence = "Chat couldn't load this workspace's chats. Restart the workspace.";
    const unreadable = (): unknown =>
      refusal(503, { detail: "mngr observe is down", user_facing_message: sentence, kind: "agent_list_unreadable" });
    const connection = stubShellConnection(true);
    mocks.sendMessage.mockRejectedValueOnce(unreadable());

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(findButton(after, "Ask an agent to fix this")).toBeUndefined();
    (findButton(after, "Report a bug")!.attrs!.onclick as () => void)();
    expect(connection.reportBug).toHaveBeenCalledExactlyOnceWith({
      message: sentence,
      details: expect.stringContaining("mngr observe is down"),
    });

    // Where the shell said Imbue Studio opens no bug report, the sentence stands alone.
    stubShellConnection(false);
    mocks.sendMessage.mockRejectedValueOnce(unreadable());
    expectNoHelpOffered(await typeAndSend(MessageInput(), "agent-1", "hello"));
  });

  it("offers neither Retry nor Restart for a chat whose account was signed out, which neither can clear", async () => {
    const sentence = "You signed out of the account this chat runs on. Choose a provider to continue it on.";
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(409, { detail: sentence, user_facing_message: sentence, kind: "account_signed_out", routine: true }),
    );

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(renderedText(after)).toContain(sentence);
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
  });

  it("offers a new chat in place of a retry for a chat that is gone", async () => {
    mocks.sendMessage.mockRejectedValueOnce(
      refusal(404, {
        detail: "Chat 'agent-1' not found",
        user_facing_message:
          "This chat is no longer here -- it was deleted. Your message is still in the box, so you can paste it " +
          "into a new chat.",
        kind: "chat_gone",
        routine: true,
      }),
    );
    mocks.selectedAccount = { id: "acct-1" };
    stubShellConnection(true);

    const after = await typeAndSend(MessageInput(), "agent-1", "hello");

    expect(renderedText(after)).toContain("This chat is no longer here -- it was deleted.");
    expectNoHelpOffered(after);
    expect(findButton(after, "Retry")).toBeUndefined();
    expect(findButton(after, "Restart")).toBeUndefined();
    (findButton(after, "Start a new chat")!.attrs!.onclick as () => void)();
    expect(mocks.startChatOnAccount).toHaveBeenCalledWith("acct-1");
  });

  it("lets Stop be tried again when it fails", async () => {
    stubShellConnection(true);
    mocks.agent.activity_state = "THINKING";
    // The chat app's answer to an exception no route expected.
    mocks.drainToComposer.mockRejectedValueOnce(refusal(500, { detail: "Internal server error: boom" }));
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    await (findByAttr(render(), "aria-label", "Interrupt agent")!.attrs!.onclick as () => Promise<void>)();

    const failed = render();
    expect(renderedText(findByClass(failed, "modal-message"))).toContain("Couldn't stop the agent. Try again.");
    expect(findButton(failed, "Ask an agent to fix this")).toBeDefined();
    (findButton(failed, "Try again")!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.drainToComposer).toHaveBeenCalledTimes(2);
    expect(renderedText(render())).not.toContain("Couldn't stop the agent");
  });

  it("does not call a switch that already finished a failure to cancel it", async () => {
    mocks.switching.handoff = handoffStateFixture({ phase: "summarizing" });
    const sentence = "This chat has already finished moving -- there's nothing to cancel.";
    mocks.cancelHandoff.mockRejectedValueOnce(
      refusal(400, {
        detail: "not moving",
        user_facing_message: sentence,
        kind: "nothing_to_cancel",
        routine: true,
      }),
    );
    const component = MessageInput();
    const render = () => component.view!({ attrs: { chatId: "agent-1" } } as never);
    (findByAttr(render(), "aria-label", "Cancel switch")!.attrs!.onclick as () => void)();
    await flushAsync();
    mocks.switching.handoff = null;

    const text = renderedText(render());
    expect(text).toContain(sentence);
    expect(text).not.toContain("Couldn't cancel");
  });

  it("offers the agent's terminal from the notice for a command chat does not take", async () => {
    const { component, turned } = composerWithSourceView();

    const after = await typeAndSend(component, "agent-1", "/status");

    (findButton(after, "Turn on Source view")!.attrs!.onclick as () => void)();
    expect(turned()).toBe(1);
  });
});

describe("MessageInput first send whose launch fails", () => {
  beforeEach(() => {
    mocks.sendMessage.mockReset();
    mocks.launchChat.mockReset();
    mocks.launchChat.mockResolvedValue({});
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
    };
    mocks.selectedAccount = { id: "acct-1" };
  });

  afterEach(() => {
    mocks.isChatRegistered = true;
    mocks.provisional = undefined;
    mocks.selectedAccount = null;
  });

  it("launches again on Try again rather than sending to a chat no launch was made for", async () => {
    mocks.launchChat.mockRejectedValueOnce(refusal(500, { detail: "mngr could not read the template" }));
    const component = MessageInput();

    const after = await typeAndSend(component, "agent-1", "Let's build something");

    expect(renderedText(after)).toContain("This chat couldn't be started. Your message is back in the box.");
    expect(findButton(after, "Restart")).toBeUndefined();
    (findButton(after, "Try again")!.attrs!.onclick as () => void)();
    await flushAsync();
    expect(mocks.launchChat).toHaveBeenCalledTimes(2);
    expect(mocks.launchChat).toHaveBeenLastCalledWith("agent-1", "acct-1", "Let's build something", "agent");
    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(localStorage.getItem("message-text:agent-1") ?? "").not.toContain("Let's build something");
  });

  it("shows the chat app's sentence for a refused launch, with an agent to ask about it", async () => {
    stubShellConnection(true);
    mocks.launchChat.mockRejectedValueOnce(
      refusal(400, { detail: "bad template", user_facing_message: "This chat couldn't be started. Try again." }),
    );

    const after = await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    const text = renderedText(after);
    expect(text).toContain("This chat couldn't be started. Try again.");
    expect(text).toContain("Your message is back in the box.");
    expect(findButton(after, "Ask an agent to fix this")).toBeDefined();
  });

  it("offers no agent for a launch the chat app refused as ordinary operation", async () => {
    stubShellConnection(true);
    const sentence = "That account is signed out. Sign in to it again, or pick another one.";
    mocks.launchChat.mockRejectedValueOnce(
      refusal(400, { detail: "no such account", user_facing_message: sentence, routine: true }),
    );

    const after = await typeAndSend(MessageInput(), "agent-1", "Let's build something");

    expect(renderedText(findByClass(after, "modal-message"))).toContain(sentence);
    expectNoHelpOffered(after);
  });
});

describe("MessageInput offers a way out of a chat whose agent cannot start", () => {
  const START_FAILED_SENTENCE = "This chat's agent couldn't be started. Try again.";
  const startFailed = (): Error =>
    refusal(500, {
      detail: "Could not start agent 'Chat-1' for a send: mngr start exited 1",
      user_facing_message: START_FAILED_SENTENCE,
      kind: "agent_start_failed",
    });
  const UNREACHABLE_SENTENCE = "The agent isn't running and didn't start.";
  const RESTART_SENTENCE = "Restart it to send your message.";
  /** A send mngr refused because the chat's agent was down and would not start. */
  const unreachable = (): Error =>
    refusal(500, {
      detail: "Agent 'Chat-1' is not running and could not be started",
      user_facing_message: UNREACHABLE_SENTENCE,
      kind: "agent_unreachable",
    });

  beforeEach(() => {
    mocks.sendMessage.mockReset();
    mocks.sendMessage.mockResolvedValue("ok");
    mocks.interruptAgent.mockReset();
    mocks.interruptAgent.mockResolvedValue(undefined);
    mocks.openProviderChooser.mockReset();
    mocks.beginSwitchToAccountId.mockClear();
    mocks.openChatPage.mockClear();
    mocks.refuseSendToFailedChat.mockReset();
    mocks.chats = [
      chatSnapshotFixture("chat-main", { title: "Main chat" }),
      chatSnapshotFixture("chat-other", { last_messaged_at: 900 }),
    ];
    mocks.mainChatId = "chat-main";
    mocks.agent.account_id = "acct-1";
    mocks.agent.harness = "claude";
    mocks.agent.activity_state = undefined;
    mocks.getComposerAttachments.mockReturnValue([]);
    localStorage.clear();
  });

  afterEach(() => {
    mocks.chats = [];
    mocks.mainChatId = null;
    mocks.agent.account_id = null;
    mocks.isChatRegistered = true;
    mocks.provisional = undefined;
  });

  /** A composer whose send to ``agent-1`` was just refused with ``error``. */
  async function composerAfterRefusedSend(error: unknown): Promise<() => unknown> {
    mocks.sendMessage.mockRejectedValueOnce(error);
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "hello");
    return () => component.view!({ attrs: { chatId: "agent-1" } } as never);
  }

  /** A composer whose send to ``agent-1`` was refused with ``reason`` because the chat never became an agent:
   *  ``failedCreate`` is the record of its failed create, absent for a chat that was closed before it started. */
  async function composerAfterSendToChatNotStarted(
    reason: string,
    failedCreate: { account_id: string } | undefined,
  ): Promise<() => unknown> {
    mocks.isChatRegistered = false;
    mocks.provisional =
      failedCreate === undefined
        ? undefined
        : {
            chat_id: "agent-1",
            name: "Chat 7",
            account_id: failedCreate.account_id,
            kind: "agent",
            phase: "failed",
            error: reason,
            is_seeded: false,
            held_sends: [],
            model_pick: null,
          };
    mocks.refuseSendToFailedChat.mockImplementationOnce(() => {
      throw new ChatNotStartedError(reason);
    });
    const component = MessageInput();
    await typeAndSend(component, "agent-1", "hello");
    return () => component.view!({ attrs: { chatId: "agent-1" } } as never);
  }

  /** A composer whose send to ``agent-1`` was refused because the chat's create had failed. */
  function composerAfterSendToFailedCreate(): Promise<() => unknown> {
    return composerAfterSendToChatNotStarted("mngr create exited with code 3", { account_id: "acct-1" });
  }

  function press(tree: unknown, label: string): void {
    const button = findButton(tree, label);
    expect(button, `"${label}" should be offered`).toBeDefined();
    (button!.attrs!.onclick as () => void)();
  }

  /** The notice's buttons in order: what each reads, and whether it is greyed out. */
  function noticeButtons(tree: unknown): { label: string; isDisabled: boolean }[] {
    return flatten(findByClass(tree, "modal-card"))
      .filter((vnode) => vnode.tag === "button")
      .map((vnode) => ({
        label: renderedText(vnode).trim(),
        isDisabled: vnode.attrs?.["aria-disabled"] === "true",
      }));
  }

  it("moves the chat off the account its agent would not start on, with the message still in the box", async () => {
    const render = await composerAfterRefusedSend(startFailed());

    press(render(), "Switch provider");

    expect(findByClass(render(), "modal-message")).toBeUndefined();
    expect(mocks.openProviderChooser).toHaveBeenCalledOnce();
    const intent = mocks.openProviderChooser.mock.calls[0][0] as {
      onSignedIn: (accountId: string) => void;
      unpickable: unknown;
    };
    expect(intent.unpickable).toEqual({ accountId: "acct-1", reason: "failing" });
    intent.onSignedIn("acct-2");
    expect(mocks.beginSwitchToAccountId).toHaveBeenCalledExactlyOnceWith("agent-1", "acct-2");
    expect(findByTag(render(), "textarea")?.attrs?.value).toBe("hello");
  });

  it("offers the switch only once the chat list names the chat, which the switch moves", async () => {
    mocks.isChatRegistered = false;
    const render = await composerAfterRefusedSend(startFailed());

    expect(findButton(render(), "Switch provider")).toBeUndefined();
    expect(findButton(render(), "Ask an agent to fix this")).toBeDefined();

    mocks.isChatRegistered = true;

    expect(findButton(render(), "Switch provider")).toBeDefined();
  });

  it.each([
    ["nothing answered", () => noAnswer()],
    ["the chat is gone", () => refusal(404, { detail: "no such chat", kind: "chat_gone" })],
  ])("offers neither a switch nor an agent when %s", async (_case, error) => {
    const tree = (await composerAfterRefusedSend(error()))();

    expect(findByClass(tree, "modal-message")).toBeDefined();
    expect(findButton(tree, "Switch provider")).toBeUndefined();
    expect(findButton(tree, "Ask an agent to fix this")).toBeUndefined();
  });

  it.each([
    ["the agent's input is blocked", () => refusal(500, { detail: "a dialog is open", kind: "input_blocked" })],
    ["nothing classified the refusal", () => refusal(500, { detail: "the host hung up" })],
  ])("offers no switch when %s, and asks an agent through the shell, not a running chat", async (_case, error) => {
    const connection = stubShellConnection(true);
    vi.mocked(connection.askAgent).mockResolvedValueOnce({ isOpened: true });
    const render = await composerAfterRefusedSend(error());
    mocks.sendMessage.mockClear();

    expect(findButton(render(), "Switch provider")).toBeUndefined();
    press(render(), "Ask an agent to fix this");
    await flushAsync();

    expect(connection.askAgent).toHaveBeenCalledTimes(1);
    expect(mocks.sendMessage).not.toHaveBeenCalled();
  });

  it("offers an agent, and no switch, for a send to a chat whose create failed", async () => {
    const tree = (await composerAfterSendToFailedCreate())();

    expect(findButton(tree, "Ask an agent to fix this")).toBeDefined();
    expect(findButton(tree, "Switch provider")).toBeUndefined();
    expect(renderedText(findByClass(tree, "notice-dismiss")).trim()).toBe("Cancel");
  });

  it("offers an agent beside Restart for an agent that is down and would not start, on the notice as it was", async () => {
    const tree = (await composerAfterRefusedSend(unreachable()))();

    expect(noticeParagraphs(tree)).toEqual([UNREACHABLE_SENTENCE, RESTART_SENTENCE]);
    expect(noticeButtons(tree)).toEqual([
      { label: "Cancel", isDisabled: false },
      { label: "Ask an agent to fix this", isDisabled: false },
      { label: "Restart", isDisabled: false },
    ]);
  });

  it("offers an agent on a sibling's notice for an agent that is down and would not start", async () => {
    const component = MessageInput();
    raiseFailureNotice("agent-1", {
      title: "Couldn't send the queued messages",
      describe: describeTapFailure,
      error: unreachable(),
      kind: "agent_unreachable",
    });
    const render = (): unknown => component.view!({ attrs: { chatId: "agent-1" } } as never);

    expect(noticeButtons(render()).map((button) => button.label)).toEqual(["Cancel", "Ask an agent to fix this"]);
    press(render(), "Ask an agent to fix this");
    await flushAsync();

    const [askedChatId, task] = mocks.sendMessage.mock.calls[0] as unknown as [string, string];
    expect(askedChatId).toBe("chat-main");
    expect(task).toContain("The chat \"Chat 1\" couldn't take a message, because its agent couldn't be started.");
    expect(task).toContain("Agent 'Chat-1' is not running and could not be started");
  });

  it.each([
    [
      "its create failed for want of a sign-in, which an agent cannot make",
      "No provider account is signed in for this workspace.",
      { account_id: "" },
    ],
    ["it was closed before it started, so no create failed", "The chat was closed before it started", undefined],
  ])("offers no agent for a send to a chat that never started when %s", async (_case, reason, failedCreate) => {
    const tree = (await composerAfterSendToChatNotStarted(reason, failedCreate))();

    expect(findByClass(tree, "modal-message")).toBeDefined();
    expect(findButton(tree, "Ask an agent to fix this")).toBeUndefined();
    expect(findButton(tree, "Switch provider")).toBeUndefined();
    expect(renderedText(findByClass(tree, "notice-dismiss")).trim()).toBe("OK");
  });

  it("asks the main chat to fix a failed create, naming the chat and its reason", async () => {
    const render = await composerAfterSendToFailedCreate();

    press(render(), "Ask an agent to fix this");
    await flushAsync();

    expect(mocks.sendMessage).toHaveBeenCalledExactlyOnceWith(
      "chat-main",
      '/assist Imbue Studio showed the user this failure: The chat "Chat 7" couldn\'t be started.\n\n' +
        "Details:\n```\nmngr create exited with code 3\n```\n\n" +
        "Find out why it failed and fix it, then tell the user what was wrong and what you changed.",
      expect.any(String),
    );
  });

  it.each([
    [
      "could not be started for the send",
      startFailed,
      "Retry",
      "Could not start agent 'Chat-1' for a send: mngr start exited 1",
    ],
    ["was down and would not start", unreachable, "Restart", "Agent 'Chat-1' is not running and could not be started"],
  ])(
    "asks the main chat to fix an agent that %s, then closes the notice and shows that chat",
    async (_case, refusedWith, otherButton, reason) => {
      const render = await composerAfterRefusedSend(refusedWith());
      let accept: (status: string) => void = () => undefined;
      mocks.sendMessage.mockImplementationOnce(() => new Promise<string>((resolve) => (accept = resolve)));

      press(render(), "Ask an agent to fix this");

      const asking = render();
      expect(findButton(asking, "Ask an agent to fix this")).toBeUndefined();
      expect(findButton(asking, "Asking an agent\u2026")?.attrs?.["aria-disabled"]).toBe("true");
      expect(findButton(asking, otherButton)?.attrs?.["aria-disabled"]).toBe("true");
      const [askedChatId, task] = mocks.sendMessage.mock.calls[1] as unknown as [string, string];
      expect(askedChatId).toBe("chat-main");
      expect(task).toContain("The chat \"Chat 1\" couldn't take a message, because its agent couldn't be started.");
      expect(task).toContain(reason);
      expect(mocks.openChatPage).not.toHaveBeenCalled();

      accept("ok");
      await flushAsync();

      expect(mocks.openChatPage).toHaveBeenCalledExactlyOnceWith("chat-main");
      expect(findByClass(render(), "modal-message")).toBeUndefined();
      expect(findByTag(render(), "textarea")?.attrs?.value).toBe("hello");
    },
  );

  it.each([
    ["could not be started for the send", startFailed],
    ["was down and would not start", unreachable],
  ])(
    "asks the agent chat messaged last, never the failing chat, when the main chat's agent %s",
    async (_case, refusedWith) => {
      mocks.chats = [
        chatSnapshotFixture("agent-1", { last_messaged_at: 950 }),
        chatSnapshotFixture("chat-older", { last_messaged_at: 100 }),
        chatSnapshotFixture("chat-newer", { last_messaged_at: 500 }),
      ];
      mocks.mainChatId = "agent-1";
      const render = await composerAfterRefusedSend(refusedWith());

      press(render(), "Ask an agent to fix this");
      await flushAsync();

      expect((mocks.sendMessage.mock.calls[1] as unknown as [string])[0]).toBe("chat-newer");
    },
  );

  it("offers Report a bug in an agent's place when the failing chat is the only agent chat", async () => {
    stubShellConnection(true);
    mocks.chats = [chatSnapshotFixture("agent-1"), chatSnapshotFixture("chat-lounge", { kind: "room" })];
    mocks.mainChatId = "agent-1";

    const tree = (await composerAfterRefusedSend(startFailed()))();

    expect(noticeButtons(tree).map((button) => button.label)).toEqual([
      "Cancel",
      "Retry",
      "Switch provider",
      "Report a bug",
    ]);
  });

  it("offers Restart, and Report a bug in an agent's place, for an agent that is down when the failing chat is the only agent chat", async () => {
    stubShellConnection(true);
    mocks.chats = [chatSnapshotFixture("agent-1"), chatSnapshotFixture("chat-lounge", { kind: "room" })];
    mocks.mainChatId = "agent-1";

    const tree = (await composerAfterRefusedSend(unreachable()))();

    expect(noticeButtons(tree).map((button) => button.label)).toEqual(["Cancel", "Report a bug", "Restart"]);
  });

  const ASK_REFUSED_SENTENCE =
    'Couldn\'t ask "Main chat". The agent is waiting on something in its terminal. Answer it there and try again.';
  const ASK_REFUSED_DETAILS =
    'Asking "Main chat":\nHTTP 500\nA dialog (a permission prompt) is open in the agent\'s terminal. Clear it there.';
  const askRefused = (): Error =>
    refusal(500, {
      detail: "A dialog (a permission prompt) is open in the agent's terminal. Clear it there.",
      user_facing_message: "The agent is waiting on something in its terminal. Answer it there and try again.",
      kind: "input_blocked",
      routine: true,
    });

  it.each([
    [
      "an agent that would not start",
      () => composerAfterRefusedSend(startFailed()),
      [START_FAILED_SENTENCE],
      "HTTP 500\nCould not start agent 'Chat-1' for a send: mngr start exited 1",
    ],
    [
      "an agent that was down and would not start",
      () => composerAfterRefusedSend(unreachable()),
      [UNREACHABLE_SENTENCE, RESTART_SENTENCE],
      "HTTP 500\nAgent 'Chat-1' is not running and could not be started",
    ],
    [
      "a chat whose create failed",
      () => composerAfterSendToFailedCreate(),
      ["Your message wasn't delivered. Your text is back in the box, so nothing was lost."],
      "mngr create exited with code 3",
    ],
  ])(
    "says under the failure of %s that the chat it asked could not be asked, with one See details for both",
    async (_case, composer, failureSentences, failureDetails) => {
      const render = await composer();
      mocks.sendMessage.mockRejectedValueOnce(askRefused());

      press(render(), "Ask an agent to fix this");
      await flushAsync();

      const refused = render();
      expect(noticeParagraphs(refused)).toEqual([...failureSentences, ASK_REFUSED_SENTENCE]);
      expect(detailsShown(refused)).toEqual([`${failureDetails}\n\n${ASK_REFUSED_DETAILS}`]);
      expect(findButton(refused, "Ask an agent to fix this")?.attrs?.["aria-disabled"]).toBeUndefined();
      expect(mocks.openChatPage).not.toHaveBeenCalled();
    },
  );

  it("files an ask refused by a fault with the failure it was about", async () => {
    const connection = stubShellConnection(true);
    const render = await composerAfterRefusedSend(startFailed());
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "mngr message exited 1" }));
    press(render(), "Ask an agent to fix this");
    await flushAsync();

    press(render(), "Report a bug");

    expect(connection.reportBug).toHaveBeenCalledExactlyOnceWith({
      message: `${START_FAILED_SENTENCE} Couldn't ask "Main chat". Try again.`,
      details:
        "HTTP 500\nCould not start agent 'Chat-1' for a send: mngr start exited 1\n\n" +
        'Asking "Main chat":\nHTTP 500\nmngr message exited 1',
    });
  });

  it("offers a draft and a bug report once a refused ask owes help, on a notice that had neither", async () => {
    const connection = stubShellConnection(true);
    const render = await composerAfterSendToFailedCreate();
    expect(findButton(render(), "Report a bug")).toBeUndefined();
    mocks.sendMessage.mockRejectedValueOnce(refusal(500, { detail: "mngr message exited 1" }));

    press(render(), "Ask an agent to fix this");
    await flushAsync();

    expect(noticeParagraphs(render())).toEqual([
      "Your message wasn't delivered. Your text is back in the box, so nothing was lost.",
      'Couldn\'t ask "Main chat". Try again.',
    ]);
    expect(findButton(render(), "Report a bug")).toBeDefined();

    // The draft is the shell's to make: it leaves the request in a chat's message box, and sends nothing.
    mocks.sendMessage.mockClear();
    vi.mocked(connection.askAgent).mockResolvedValueOnce({ isOpened: true });
    press(render(), "Draft it in a chat");
    await flushAsync();

    expect(mocks.sendMessage).not.toHaveBeenCalled();
    expect(connection.askAgent).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/^Imbue Studio showed the user this failure: The chat "Chat 7" couldn't be started\./),
      { showsOutcome: true },
      "draft",
    );
    expect(findByClass(render(), "modal-message")).toBeUndefined();
  });

  it("leaves no button of the notice to press while the ask is on its way, and gives them back once it is refused", async () => {
    stubShellConnection(true);
    const render = await composerAfterRefusedSend(startFailed());
    let refuse: (error: unknown) => void = () => undefined;
    mocks.sendMessage.mockImplementationOnce(() => new Promise<string>((_accept, reject) => (refuse = reject)));

    press(render(), "Ask an agent to fix this");

    expect(noticeButtons(render())).toEqual([
      { label: "Cancel", isDisabled: true },
      { label: "Retry", isDisabled: true },
      { label: "Switch provider", isDisabled: true },
      { label: "Asking an agent\u2026", isDisabled: true },
    ]);

    refuse(askRefused());
    await flushAsync();

    // The chat that was asked is only waiting on its terminal, which owes no help: the ask is there to make again,
    // with no draft or bug report in its place.
    expect(noticeButtons(render())).toEqual([
      { label: "Cancel", isDisabled: false },
      { label: "Retry", isDisabled: false },
      { label: "Switch provider", isDisabled: false },
      { label: "Ask an agent to fix this", isDisabled: false },
    ]);
  });

  it("leaves no button of the notice to press while a retry is on its way", async () => {
    stubShellConnection(true);
    const render = await composerAfterRefusedSend(startFailed());
    mocks.sendMessage.mockImplementationOnce(() => new Promise<string>(() => undefined));

    press(render(), "Retry");

    expect(noticeButtons(render())).toEqual([
      { label: "Cancel", isDisabled: true },
      { label: "Retrying\u2026", isDisabled: true },
      { label: "Switch provider", isDisabled: true },
      { label: "Ask an agent to fix this", isDisabled: true },
    ]);
  });

  it("drops what a refused ask said once Restart is pressed, whose own failure it would sit under", async () => {
    const render = await composerAfterRefusedSend(unreachable());
    mocks.sendMessage.mockRejectedValueOnce(askRefused());
    press(render(), "Ask an agent to fix this");
    await flushAsync();
    expect(noticeParagraphs(render())).toContain(ASK_REFUSED_SENTENCE);
    mocks.interruptAgent.mockRejectedValueOnce(refusal(500, { detail: "mngr stop exited 1" }));

    press(render(), "Restart");
    await flushAsync();

    expect(noticeParagraphs(render())).toEqual([
      "This chat's agent could not be restarted, so your message was not resent. Try again.",
    ]);
    expect(detailsShown(render())).toEqual(["HTTP 500\nmngr stop exited 1"]);
  });

  it("asks again with the failure's own details, not what the refused ask said", async () => {
    const render = await composerAfterRefusedSend(startFailed());
    mocks.sendMessage.mockRejectedValueOnce(askRefused());
    press(render(), "Ask an agent to fix this");
    await flushAsync();

    press(render(), "Ask an agent to fix this");
    await flushAsync();

    const task = (mocks.sendMessage.mock.calls[2] as unknown as [string, string])[1];
    expect(task).toContain("Could not start agent 'Chat-1' for a send: mngr start exited 1");
    expect(task).not.toContain("A dialog (a permission prompt) is open");
  });
});
