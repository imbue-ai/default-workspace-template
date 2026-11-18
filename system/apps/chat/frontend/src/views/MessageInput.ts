import m from "mithril";
import { askAgentToFix } from "@imbue/workspace-ui/src/app_contract";
import type { AgentFixRequest, ShownFailure } from "@imbue/workspace-ui/src/app_contract";
import { ASKING_AN_AGENT_LABEL, ASK_AGENT_TO_FIX_LABEL } from "@imbue/workspace-ui/src/components/FailureButtons";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { NoticeAction, NoticeDialogAttrs } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { SendFailureKind } from "@imbue/workspace-ui/src/models/request-error";
import { failureSentence } from "@imbue/workspace-ui/src/models/failure";
import { startChatOnAccount } from "../shell";
import { getShellConnection } from "../shellConnection";
import { askConnectionToChat, chatToAskAbout, chatToAskAboutFailedCreate, withRefusedAsk } from "./ask-running-agent";
import { reportBugActions } from "./failure-actions";
import { SWITCH_PROVIDER_LABEL, chooseProviderToSwitchChat } from "./provider-switch";
import {
  DECLINED_COMPACT_TITLE,
  composerFailureKindOf,
  declinedCompactSentence,
  describeCancelSwitchFailure,
  describeLaunchFailure,
  describeNewChatFailure,
  describeSendFailure,
  describeStopFailure,
  describeSwitchFailure,
  isDeclinedCompact,
  isNothingToCancel,
} from "./composer-failures";
import type { ComposerFailureKind } from "./composer-failures";
import { plainFailureLines } from "./failure-lines";
import type { FailureLines } from "./failure-lines";
import {
  clearComposerAttachments,
  getComposerAttachments,
  getReadyAttachmentPaths,
  hasReadyAttachments,
  removeComposerAttachment,
  restoreComposerAttachments,
  uploadFilesToComposer,
  waitForComposerUploads,
} from "../models/ComposerAttachments";
import type { ComposerAttachment } from "../models/ComposerAttachments";
import { matchesKeybinding } from "../models/keybindings";
import { buildMessageWithAttachments, formatFileSize } from "../models/attachments";
import {
  forgetDictatedSpeech,
  hasDictatedSpeech,
  markDictatedSpeech,
  withDictationNote,
  withoutDictationNotes,
} from "../models/dictationNote";
import { stageElementReferences } from "../models/elementReferences";
import {
  drainToComposer,
  getEventsForChat,
  hasUserTurnLandedSince,
  interruptAgent,
  mintMessageId,
  sendMessage,
  sentUserTurnIds,
} from "../models/Response";
import type { SendStatus } from "../models/Response";
import { cancelHandoff, switchChat } from "../models/Handoffs";
import { addModelChoiceFailureListener } from "../models/ModelSettings";
import {
  getPendingPick,
  isSwitchSending,
  nextSendSwitchTarget,
  setPendingAccount,
  setSwitchSending,
} from "../models/PendingLane";
import type { ProviderAccount } from "../models/Providers";
import {
  beginSwitchToAccountId,
  isRefusedForNewChat,
  launchAwaitingChatAsRoom,
  openSwitchDialog,
  startNewChatWithDraft,
} from "./SwitchDialog";
import {
  addOutgoing,
  clearOutgoing,
  dropOutgoing,
  dropOutgoingNoTurnWillRetire,
  getOutgoingMessages,
  noteOutgoingAccepted,
} from "../models/OutgoingMessages";
import {
  describeRequestError,
  describeRequestErrorKind,
  describeRequestFailure,
} from "@imbue/workspace-ui/src/models/request-error";
import {
  accountForFirstSend,
  areAccountsLoaded,
  getSelectedAccount,
  isAccountSignedOut,
  loadAccounts,
  openProviderChooser,
  whenAccountsReadyToChoose,
} from "../models/Providers";
import type { DiscardedChat, ProvisionalChat } from "../models/Chats";
import {
  ensureHarnessCatalogs,
  findComposerPopup,
  getHarnessCatalog,
  hasFastModeLimit,
} from "../models/HarnessCatalog";
import { chooseFastMode, parseFastModeCommand } from "./fast-mode-limit";
import {
  ChatNotStartedError,
  addProvisionalChatDiscardedListener,
  getChatById,
  getProvisionalChat,
  refuseSendToFailedChat,
  isHandoffCancellable,
  isLaunchInFlight,
  isRoomChat,
  launchChat,
  whenChatRegistered,
} from "../models/Chats";
import { COMPACTING_STATE, isWorkingActivityState } from "../models/activityState";
import { browserWakeLockHost, createScreenWakeLock } from "../models/screenWakeLock";
import type { ScreenWakeLock } from "../models/screenWakeLock";
import { isChatConnecting } from "./ConnectingIndicator";
import { isAgentProcessDead } from "./agentLiveness";
import { apiUrl, getBasePath } from "@imbue/workspace-ui/src/base-path";
import { harnessLabel } from "./harness-labels";
import { handoffComposerPlaceholder } from "./handoff-phase";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { ensureMentionHandles, matchingHandles } from "../models/Mentions";
import type { MentionHandle } from "../models/Mentions";
import { MENTION_ROW_CAP, insertMention, mentionDraftAt, mentionPopover } from "./MentionPopover";
import type { MentionDraft } from "./MentionPopover";
import { icon, spinnerIcon, stopIcon } from "@imbue/workspace-ui/src/components/icons";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { appPartAttrs } from "@imbue/workspace-ui/src/themes/parts";
import { ATTACHMENT_CHIP_HEIGHT_CLASS, AttachmentStrip } from "./AttachmentStrip";
import { keydownMatchesChords } from "@imbue/workspace-ui/src/keybindings";
import { TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import { QueryObserver, skipToken } from "@tanstack/query-core";
import {
  createDictation,
  describeSpeechModelLoadError,
  isSpeechModelLoadProblem,
} from "@imbue/workspace-voice/src/dictation";
import type { Dictation, DictationProblem, DictationState } from "@imbue/workspace-voice/src/dictation";
import type { DeviceClass } from "@imbue/workspace-voice/src/engines";
import { probeEngineDownload } from "@imbue/workspace-voice/src/downloadProbe";
import { appendFinal, clearTentative, openSpan, renderSpan, setTentative } from "@imbue/workspace-voice/src/liveSpan";
import type { LiveSpan } from "@imbue/workspace-voice/src/liveSpan";
import { createReadyChime } from "@imbue/workspace-voice/src/readyChime";
import { getSpeechModels, getVoiceQueryClient } from "@imbue/workspace-voice/src/speechModel";
import type { SpeechModel, SpeechModelProgress } from "@imbue/workspace-voice/src/speechModel";
import {
  describeHost,
  dictationShortcutChord,
  dictationShortcutLabel,
  isApplePlatform,
  isDictationSupported,
  pageDictationOptions,
} from "@imbue/workspace-voice/src/support";
import type { PageDictationOptions } from "@imbue/workspace-voice/src/support";
import { DictationButton, dictationNotice, dictationProgressPercent } from "./DictationButton";
import type { DictationButtonState } from "./DictationButton";

export const MAX_TEXTAREA_HEIGHT_PX = 240;

/* Styling.
 * Utilities in the markup; the message-input and composer-attachment class
 * names stay as bare markers (the e2e tests drive the textbox and send button
 * by them). Attachment status looks are resolved in code, one utility per
 * property. */

/** The composer card. No shadow: it sits at the foot of its own pane
 *  rather than floating over the transcript. Focus thickens its border to 2px
 *  in the accent, taking the extra pixel out of the padding so nothing moves.
 *  Its corners are concentric with the buttons and chips inside: their 6px
 *  radius plus the padding and the border. */
const INPUT_BOX_CLASS =
  "message-input-box flex flex-col gap-0.5 rounded-[13px] border bg-composer p-1.5 " +
  "[transition:border-color_150ms] focus-within:border-2 focus-within:border-accent focus-within:p-[5px]";

/** The text and its buttons: paperclip | text | toolbar. ``fitComposer`` sets ``data-stacked`` on the row once
 *  the text needs a second line or there are attachments (``data-attachments``); the text then takes the full
 *  width, with the paperclip and the toolbar on a row under it, and any attachments on a row between the two. */
const MESSAGE_ROW_CLASS =
  "message-input-row group/row grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-0.5 " +
  "data-[stacked]:gap-y-0.5";

const ATTACH_BUTTON_CLASS =
  "message-input-attach-button col-start-1 row-start-1 shrink-0 group-data-[stacked]/row:row-start-2 " +
  "group-data-[attachments]/row:row-start-3!";

const TOOLBAR_CLASS =
  "message-input-toolbar col-start-3 row-start-1 flex shrink-0 items-center gap-1 justify-self-end " +
  "group-data-[stacked]/row:row-start-2 group-data-[attachments]/row:row-start-3!";

const ATTACHMENT_DETAIL_CLASS = "composer-attachment-detail truncate text-[11px] leading-[1.3]";

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";
const COMPOSER_HINT_KEY_PREFIX = "composer-hint:";

// Kinds of refused send that no retry or restart can clear: the chat's account was signed out, the chat is gone, or it
// never became an agent (its create failed, or it was closed first), which leaves no agent to send to.
const KINDS_NOTHING_HERE_CAN_CLEAR: ReadonlySet<ComposerFailureKind> = new Set([
  "account_signed_out",
  "chat_gone",
  "chat_not_started",
]);
// Kinds of refused send whose cause the agent's terminal shows.
const KINDS_THE_TERMINAL_SHOWS: ReadonlySet<ComposerFailureKind> = new Set([
  "input_blocked",
  "submission_unconfirmed",
]);
// Kinds a restart must not answer: an agent still starting would lose the session it was about to finish bringing up;
// one that may already be working on the message would have that turn cut off and get it twice; an agent whose start
// just failed has nothing running to restart, and Retry starts it again; and a kind the composer does not know says
// nothing about the agent.
const KINDS_RESTART_MUST_NOT_ANSWER: ReadonlySet<ComposerFailureKind> = new Set([
  "not_ready",
  "agent_start_failed",
  "submission_unconfirmed",
  "not_answered",
  "unrecognised",
]);
// Kinds of send that may have reached the agent: its turn arriving takes the copy back out of the composer.
const KINDS_THAT_MAY_HAVE_ARRIVED: ReadonlySet<ComposerFailureKind> = new Set([
  "submission_unconfirmed",
  "not_answered",
]);

/** The running chat the notice of a refused send offers to ask, for a send whose chat has no agent it could start:
 *  what stopped it may be the workspace itself, which an agent that is already running can look into. A chat that
 *  was closed before it started has no failed create to look into. */
function chatToAskAboutRefusedSend(kind: ComposerFailureKind, failingChatId: string): string | null {
  if (kind === "agent_start_failed") return chatToAskAbout(failingChatId);
  if (kind !== "chat_not_started") return null;
  const provisional = getProvisionalChat(failingChatId);
  return provisional?.phase === "failed" ? chatToAskAboutFailedCreate(provisional) : null;
}

// A NUL character cannot be typed into a terminal, so an agent that takes its input as keystrokes
// refuses the whole message over one. A textarea does not show NULs anyway (they arrive by pasting
// binary content), so dropping them sends exactly what the user saw.
function withoutNulCharacters(draft: string): string {
  return draft.split("\0").join("");
}

function messageTextKey(chatId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${chatId}`;
}

function composerHintKey(chatId: string): string {
  return `${COMPOSER_HINT_KEY_PREFIX}${chatId}`;
}

// Blocks handed back to the composer from OUTSIDE this component (a native shoulder tap whose combined
// resend failed -- see QueuedMessageView), keyed by agent. The composer's own ``messageText`` is a
// per-instance closure, so a sibling view cannot merge into it directly; it drops the block here and
// redraws, and the composer applies it on its next view pass (prepend, then persist), the same
// merge-not-drop rule Stop's drain-to-composer uses. Persisted to localStorage regardless, so the text
// survives even if the composer is not currently mounted -- never swallowed (contract A1a).
const pendingComposerPrepends = new Map<string, string>();

// Added to the notice's sentence only when it offers Restart.
const SEND_FAILURE_RESTART_SENTENCES: Partial<Record<ComposerFailureKind, string>> = {
  input_blocked: "Or restart the agent.",
  agent_unreachable: "Restart it to send your message.",
};

// The hint a chat's composer shows as its placeholder until its next send (the multiplayer spec, section 9.2:
// "How do you want to change <app>?"), by chat; set from outside the component, as the prepends are, and taken
// up on the composer's next view pass, which also focuses it once. Persisted to localStorage as the prepends
// are: the chat root sets it from its own document, and the page's composer (another document, with a map of
// its own) reads it from there when it mounts.
const composerHints = new Map<string, string>();
const pendingComposerFocuses = new Set<string>();

/** A failure raised from OUTSIDE this component, waiting for the composer to show it. */
interface PendingFailureNotice {
  title: string;
  /** What the notice says about ``error``; asked again with the next error when Try again fails too. */
  describe: (error: unknown) => FailureLines;
  error: unknown;
  /** mngr's classification, so a sibling's failure earns the same buttons the composer's would. */
  kind?: SendFailureKind;
  /** Repeated by Try again. Omitted when the operation cannot be repeated. */
  retry?: () => Promise<void>;
  /** Another way forward, offered beside Try again. */
  action?: NoticeAction;
}
const pendingFailureNotices = new Map<string, PendingFailureNotice>();

/**
 * Raise the chat's failure notice for ``chatId`` from a sibling view.
 *
 * The composer owns the notice because it owns the composer -- Cancel means "the message is back
 * in the box, go look at it". A sibling that fails a send hands the failure here rather than
 * putting up its own dialog, so one shape of failure gets one shape of answer no matter which
 * button started it.
 */
export function raiseFailureNotice(chatId: string, notice: PendingFailureNotice): void {
  // Only ever one pending per agent, and only for the agent it concerns: a composer that is not
  // mounted cannot show this, and a stale entry would otherwise surface as a modal about
  // something that failed long ago the next time the user opened that chat.
  pendingFailureNotices.clear();
  pendingFailureNotices.set(chatId, notice);
  m.redraw();
}

/** Tell a new chat started from inside ``chatId`` (the switch dialog's or the failed-switch notice's "Start a new
 *  chat") that was refused or got no answer, with ``retry`` to start it again. A create nothing answered may have
 *  made the chat, so it is not offered again. */
export function raiseNewChatFailure(chatId: string, error: unknown, retry: () => Promise<void>): void {
  raiseFailureNotice(chatId, {
    title: "Couldn't start a new chat",
    describe: describeNewChatFailure,
    error,
    retry: describeRequestFailure(error).cause === "not_answered" ? undefined : retry,
  });
}

// The chats a new chat is being started in place of, so a second press of a button that stays on screen does not
// start another.
const newChatStartsInFlight = new Set<string>();

/** Open a new chat beside ``chatId``, on the account a new chat would start on, in place of a chat the app no longer
 *  has; with nothing signed in, the provider chooser. A failure is told on ``chatId``'s composer. */
export function startNewChatInPlaceOf(chatId: string): void {
  const account = getSelectedAccount();
  if (account === null) {
    openProviderChooser();
    return;
  }
  if (newChatStartsInFlight.has(chatId)) return;
  newChatStartsInFlight.add(chatId);
  const startNewChat = (): Promise<void> => startChatOnAccount(account.id);
  startNewChat()
    .catch((error: unknown) => raiseNewChatFailure(chatId, error, startNewChat))
    .finally(() => newChatStartsInFlight.delete(chatId));
}

// Chats whose draft was taken out of the composer by a sibling view (the switch dialog's "Start a
// new chat" moves it to the new chat); the mounted composer clears its own text on its next pass.
const pendingComposerClears = new Set<string>();

/** What a sibling view took out of ``chatId``'s composer to send somewhere else. */
export interface TakenComposerDraft {
  /** What the user typed, for putting back if it could not be sent. */
  text: string;
  /** What goes on the wire: the text, the dictation note when the draft may hold dictated words, and the
   *  attachment references appended. */
  finalText: string;
  attachments: readonly ComposerAttachment[];
  /** Whether dictation wrote into the draft, so one put back counts as holding dictated words again. */
  mayContainDictatedSpeech: boolean;
}

/**
 * Take ``chatId``'s draft out of the composer, leaving it empty: what a new chat started from the
 * switch dialog begins with. The attachments travel with the text, so a file dropped in this
 * composer is not left behind by the move, and in-flight uploads are awaited first so a file
 * dropped a moment ago still makes it. The persisted copy goes too, so a reload does not bring it
 * back. Null when an upload failed: the file would leave the message silently and its chip would be
 * cleared along with the rest, so this refuses and names it instead, as an ordinary send does.
 */
export async function takeComposerDraft(chatId: string): Promise<TakenComposerDraft | null> {
  await waitForComposerUploads(chatId);
  const failed = getComposerAttachments(chatId).filter((attachment) => attachment.status === "error");
  if (failed.length > 0) {
    const names = failed.map((attachment) => attachment.fileName).join(", ");
    raiseFailureNotice(chatId, {
      title: failed.length === 1 ? "An attachment didn't upload" : "Some attachments didn't upload",
      describe: () => ({
        body: [
          `${names} could not be uploaded, so the new chat was not started. Remove the attachment, or try again.`,
        ],
        details: null,
        isReportable: false,
      }),
      error: null,
    });
    return null;
  }
  const text = localStorage.getItem(messageTextKey(chatId)) ?? "";
  const attachments = getComposerAttachments(chatId);
  const mayContainDictatedSpeech = hasDictatedSpeech(chatId);
  const finalText = buildMessageWithAttachments(
    mayContainDictatedSpeech ? withDictationNote(text) : text,
    getReadyAttachmentPaths(chatId),
  );
  localStorage.removeItem(messageTextKey(chatId));
  forgetDictatedSpeech(chatId);
  clearComposerAttachments(chatId);
  pendingComposerClears.add(chatId);
  m.redraw();
  return { text, finalText, attachments, mayContainDictatedSpeech };
}

/** Hand a taken draft back to ``chatId``'s composer when what it was taken for did not happen. */
export function restoreComposerDraft(chatId: string, taken: TakenComposerDraft): void {
  restoreToComposer(chatId, taken.text, taken.attachments);
  if (taken.mayContainDictatedSpeech) markDictatedSpeech(chatId);
}

/**
 * Put ``text`` and ``attachments`` back in ``chatId``'s composer, above whatever is in it now.
 *
 * Prepending is what lets it run unconditionally: the returned message first and any draft typed
 * since after it, and neither is lost. The attachments merge rather than overwrite, since anything
 * attached in the meantime would otherwise go with them.
 */
function restoreToComposer(chatId: string, text: string, attachments: readonly ComposerAttachment[]): void {
  prependToComposer(chatId, text);
  const existingAttachments = getComposerAttachments(chatId);
  const existingIds = new Set(existingAttachments.map((attachment) => attachment.localId));
  restoreComposerAttachments(chatId, [
    ...attachments.filter((attachment) => !existingIds.has(attachment.localId)),
    ...existingAttachments,
  ]);
}

/** Show ``hint`` as ``chatId``'s composer placeholder until its next send, and focus the composer: what the chat
 *  root hands a page when an intake carried a hint (a window's "Chat about this app"), from the page's own document
 *  when it is loaded, else from the root's, where only the persisted hint reaches the page. */
export function setComposerHint(chatId: string, hint: string): void {
  if (!hint) {
    return;
  }
  localStorage.setItem(composerHintKey(chatId), hint);
  composerHints.set(chatId, hint);
  pendingComposerFocuses.add(chatId);
  m.redraw();
}

function clearComposerHint(chatId: string): void {
  composerHints.delete(chatId);
  localStorage.removeItem(composerHintKey(chatId));
}

// The page's composer box while it is mounted (a page has one), for the chat root to hand it the keyboard.
let mountedComposerTextarea: HTMLTextAreaElement | null = null;

/** Put the keyboard in the page's composer; false when there is none to take it (not mounted, or disabled). */
export function focusComposer(): boolean {
  if (mountedComposerTextarea === null || mountedComposerTextarea.disabled) {
    return false;
  }
  mountedComposerTextarea.focus();
  return true;
}

/** Hand ``block`` back to ``chatId``'s composer (prepended above any draft), from a sibling view or from outside
 *  the page. An element reference in it is attached as a file first, and only the prompt naming it goes in the
 *  text (``stageElementReferences``). Delivered text comes back without the dictation notes its messages carried:
 *  the draft it joins counts as holding dictated words instead, so the next send carries one note. */
export function prependToComposer(chatId: string, block: string): void {
  if (!block) {
    return;
  }
  const returned = withoutDictationNotes(block);
  const existingDraft = localStorage.getItem(messageTextKey(chatId)) ?? "";
  // The block already leads the draft (the app-chat button pressed again while its lead-in sits unsent):
  // prepending it once more would stack identical copies. Checked before staging, which attaches files.
  if (existingDraft.startsWith(returned.text)) {
    return;
  }
  const text = stageElementReferences(chatId, returned.text);
  if (!text) {
    return;
  }
  const merged = existingDraft.trim().length === 0 ? text : `${text}\n\n${existingDraft}`;
  localStorage.setItem(messageTextKey(chatId), merged);
  if (returned.hadNote) markDictatedSpeech(chatId);
  pendingComposerPrepends.set(chatId, merged);
  m.redraw();
}

/** Lay the composer out for its text: the buttons beside a message that fits on one line, or in a
 *  row under a longer one, so the text gets the full width. A row holding attachments
 *  (``data-attachments``) is always stacked, whatever the text's length. Wrapping is measured with the buttons
 *  beside the text whichever layout is showing, so the stacked layout's extra width cannot unwrap
 *  the text and flip it back.
 *
 *  The row holds its height while the text is measured collapsed to one line. Otherwise the page
 *  lays out around a one-line composer for that moment, and the browser clamps the transcript's
 *  scroll position to fit it: a tall composer would pull the transcript off its bottom on every
 *  redraw.
 *
 *  Once the text outgrows the cap the textarea scrolls. It keeps its scroll position across the
 *  collapse, which not every browser preserves. Typing (``isTyping``) with the caret at the end of the
 *  draft scrolls it all the way down instead, since the browser scrolls the caret into view without
 *  the padding under the last line. */
function fitComposer(row: HTMLElement, textarea: HTMLTextAreaElement, isTyping: boolean): void {
  const scrollTopPx = textarea.scrollTop;
  const isTypingAtEnd = isTyping && textarea.selectionEnd === textarea.value.length;
  row.style.height = `${row.getBoundingClientRect().height}px`;
  row.removeAttribute("data-stacked");
  textarea.style.overflowY = "hidden";
  textarea.style.height = "auto";
  row.toggleAttribute(
    "data-stacked",
    row.hasAttribute("data-attachments") || textarea.scrollHeight > textarea.clientHeight,
  );
  textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  const isOverflowing = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT_PX;
  textarea.style.overflowY = isOverflowing ? "auto" : "hidden";
  const targetScrollTopPx =
    isOverflowing && isTypingAtEnd ? textarea.scrollHeight - textarea.clientHeight : scrollTopPx;
  // Writing scrollTop is a programmatic scroll, which would interrupt the reader scrolling the draft mid-gesture.
  if (textarea.scrollTop !== targetScrollTopPx) textarea.scrollTop = targetScrollTopPx;
  row.style.height = "";
}

function imageFilesFromClipboard(clipboardData: DataTransfer | null): File[] {
  if (clipboardData === null) {
    return [];
  }
  const files: File[] = [];
  for (const item of Array.from(clipboardData.items)) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file !== null) {
        files.push(file);
      }
    }
  }
  return files;
}

// The stopped agents this page asked to start because the user began a message to them, while that start
// runs: one request per stop, not one per keystroke.
const warmingAgentIds = new Set<string>();

/**
 * Start a stopped chat's agent as the user begins a message to it, so the send finds the agent up rather
 * than waiting on it. Best-effort: when this start fails or is still running at the send, the send starts
 * the agent itself, as it always has.
 */
function warmUpStoppedChat(chatId: string): void {
  const chat = getChatById(chatId);
  const agentId = chat?.active_agent.agent_id;
  if (chat === undefined || agentId === undefined || chat.handoff !== null) return;
  if (!isAgentProcessDead(chat.active_agent.state) || warmingAgentIds.has(agentId)) return;
  warmingAgentIds.add(agentId);
  fetch(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/start`), { method: "POST" })
    .then((response) => {
      if (!response.ok) console.warn(`Could not start chat ${chatId} ahead of its message (HTTP ${response.status})`);
    })
    .catch((err: unknown) => console.warn(`Could not start chat ${chatId} ahead of its message: ${String(err)}`))
    .finally(() => warmingAgentIds.delete(agentId));
}

/** Whether this page offers dictation at all (the workspace-voice-input spec, D5): a secure context with a
 *  microphone, an audio worklet, WebAssembly, and a worker. */
function isDictationOffered(): boolean {
  return isDictationSupported(describeHost());
}

/** The kind of device this page is on, which picks the engine it dictates with (the spec's D9): touch where a
 *  finger is the primary pointer. A page with no window to ask has no pointer, and is a desktop. */
function pageDeviceClass(): DeviceClass {
  return typeof matchMedia === "function" && matchMedia(TOUCH_MEDIA_QUERY).matches ? "touch" : "desktop";
}

function isAppleKeyboard(): boolean {
  return typeof navigator !== "undefined" && isApplePlatform(navigator);
}

// The mounted composer's hook for the page going out of view, which stops its dictation, keeping the words, and
// dismisses a load of the speech model it is showing.
let mountedDictationHide: (() => void) | null = null;

// The chime that says the microphone is open: one for the page, since a browser caps the audio contexts a page
// may hold and every open chat panel mounts a composer of its own.
const readyChime = createReadyChime();

// How long a composer waits, from its mount, to learn the speech model's state before it stops drawing the mic as
// ready: a worker that neither answers nor fails would otherwise leave the state unknown for good.
const SPEECH_MODEL_STATE_WAIT_MS = 5000;

// How long after a press shows the speech model's load a second press is taken for the first one landing twice:
// a double click, or a second click that finds the label where the mic was.
const REPEATED_PRESS_WINDOW_MS = 500;

/** The dictation this page runs; the page's own URL roots the runtime's files, and a document-less test has none. */
function dictationPageOptions(): PageDictationOptions {
  const pageUrl = typeof location === "undefined" ? "" : location.href;
  return pageDictationOptions(localStorage, getBasePath(), pageUrl, pageDeviceClass());
}

/** Stop the page's dictation, keeping the words it heard, and dismiss a load of the speech model it is showing:
 *  what the page does when the shell says it is hidden. */
export function stopDictationWhenHidden(): void {
  mountedDictationHide?.();
}

export interface MessageInputAttrs {
  chatId: string | null;
  /** A control at the left of the composer's box, bottom-aligned with it: the phone layout's settings button. */
  leading?: m.Children;
  /** Turns the chat over to its agent's terminal; absent while the chat has no agent to show. */
  onTurnOnSourceView?: () => void;
}

/** A notice that stands in for the composer while the chat has no provider to run on: why, and a button that opens
 *  the provider chooser. ``markerClass`` names which notice it is. */
function providerNotice(markerClass: string, sentence: string, onChoose: () => void, onShown?: () => void): m.Vnode {
  return m(
    "div",
    {
      oncreate: onShown,
      class:
        `${markerClass} mb-2 flex flex-row items-center justify-between gap-3 rounded-[13px] border ` +
        "border-default bg-surface-secondary py-3 pr-3 pl-5 text-(length:--font-size-body) text-secondary",
    },
    [
      m("span", sentence),
      m(
        Button,
        { variant: "primary", sm: true, extra: "message-input-choose-provider shrink-0", onclick: onChoose },
        "Choose a provider",
      ),
    ],
  );
}

/** The composer's box, beside ``leading`` when there is one. The row is there either way, so the box (and the
 *  textarea in it, with its focus and the soft keyboard) is kept when ``leading`` comes or goes. */
function composeRow(leading: m.Children | undefined, boxChildren: m.Children[]): m.Vnode {
  return m("div", { class: "message-input-compose-row flex items-end gap-1.5" }, [
    leading ?? null,
    m(
      "div",
      { class: `${INPUT_BOX_CLASS} relative min-w-0 flex-1`, ...appPartAttrs("chat", "composer") },
      boxChildren,
    ),
  ]);
}

/**
 * Re-read the account list when a send was refused because the chat's account was signed out.
 *
 * The composer offers "Choose a provider" from that list, which only this page's own account
 * actions refresh: a removal made in another window leaves it listing the account until then.
 */
function catchUpOnSignedOutAccount(sendError: unknown): void {
  if (describeRequestErrorKind(sendError) !== "account_signed_out") {
    return;
  }
  loadAccounts().then(
    () => m.redraw(),
    (err: unknown) => console.error(`Failed to re-read the account list: ${describeRequestError(err)}`),
  );
}

// A pick in the model bar the chat app refused (the chat is switching providers, say) is told in the chat's notice.
addModelChoiceFailureListener((chatId, error) =>
  raiseFailureNotice(chatId, {
    title: "Couldn't change the model",
    describe: (failed) => plainFailureLines(failed, "The model couldn't be changed"),
    error,
  }),
);

/** A message this page is sending, or sent, to a chat that is not an agent yet, which the chat app holds for the
 *  agent its create lands: the words and attachments as typed, to put back if the chat is deleted first (the chat
 *  app drops what it held and says which), and the bubble standing for it. Only this page knows which of the held
 *  messages were typed here and what attachment chips they had; the chat app holds the text alone. */
interface SendToChatNotYetAnAgent {
  text: string;
  attachments: readonly ComposerAttachment[];
  outgoingId: string;
  /** A Retry's until it lands: the failed send's copy is still in the box, so nothing is put back. */
  isInBox: boolean;
  /** Its send has had its answer, so a discard after that has nothing to tell the send path. */
  isAnswered?: boolean;
}
const sendsToChatsNotYetAgents = new Map<string, Map<string, SendToChatNotYetAnAgent>>();
// Messages a discard put back before their send's own answer came, by message id.
const putBackBeforeAnswered = new Set<string>();
let isListeningForDiscardedChats = false;

/** Note a send about to go to ``chatId`` while the chat is not an agent yet, before its answer: a discard can reach
 *  the page first. */
function noteSendToChatNotYetAnAgent(chatId: string, messageId: string, send: SendToChatNotYetAnAgent): void {
  if (getChatById(chatId) !== undefined || getProvisionalChat(chatId) === undefined) return;
  for (const notedChatId of [...sendsToChatsNotYetAgents.keys()]) {
    if (getChatById(notedChatId) !== undefined) sendsToChatsNotYetAgents.delete(notedChatId);
  }
  const noted = sendsToChatsNotYetAgents.get(chatId) ?? new Map<string, SendToChatNotYetAnAgent>();
  noted.set(messageId, send);
  sendsToChatsNotYetAgents.set(chatId, noted);
  if (!isListeningForDiscardedChats) {
    isListeningForDiscardedChats = true;
    addProvisionalChatDiscardedListener(putBackSendsDroppedWithChat);
  }
}

/** Settle a noted send by its answer (null for none): one the chat app did not hold is forgotten. Whether the chat's
 *  discard already put it back in the box, so the caller does not put it back again. */
function settleSendToChatNotYetAnAgent(chatId: string, messageId: string, status: SendStatus | null): boolean {
  if (putBackBeforeAnswered.delete(messageId)) return true;
  const noted = sendsToChatsNotYetAgents.get(chatId);
  const send = noted?.get(messageId);
  if (status !== "held") noted?.delete(messageId);
  else if (send !== undefined) send.isAnswered = true;
  return false;
}

/** A noted Retry landed and its copy left the box, so the chat's discard has to put it back. */
function noteSendLeftBox(chatId: string, messageId: string): void {
  const send = sendsToChatsNotYetAgents.get(chatId)?.get(messageId);
  if (send !== undefined) send.isInBox = false;
}

function putBackSendsDroppedWithChat(discarded: DiscardedChat): void {
  const chatId = discarded.chat_id;
  const noted = sendsToChatsNotYetAgents.get(chatId);
  sendsToChatsNotYetAgents.delete(chatId);
  if (noted === undefined) return;
  // What the chat app dropped, in the order it held them: a send Stop took back is not among them.
  const dropped = discarded.held_sends.flatMap((held) => {
    const send = noted.get(held.message_id);
    return send === undefined ? [] : [{ messageId: held.message_id, send, heldText: held.text }];
  });
  if (dropped.length === 0) return;
  // Last first: each put-back goes above what is in the box, so the box reads in the order they were sent.
  for (const { messageId, send, heldText } of [...dropped].reverse()) {
    if (send.isAnswered !== true) putBackBeforeAnswered.add(messageId);
    dropOutgoing(chatId, send.outgoingId);
    if (send.isInBox) continue;
    restoreToComposer(chatId, send.text, send.attachments);
    // The held text is the message as it was sent, so its note says the words put back may be dictated.
    if (withoutDictationNotes(heldText).hadNote) markDictatedSpeech(chatId);
  }
  raiseFailureNotice(chatId, {
    title: "Couldn't send your message",
    describe: describeSendFailure,
    error: new ChatNotStartedError("The chat was closed before it started"),
  });
}

/** The provider chooser for a chat whose account was signed out: the account signed in to or picked is the one the
 *  chat's next message moves it to. */
function chooseProviderToContinue(chatId: string): void {
  openProviderChooser({ onSignedIn: (chosen) => beginSwitchToAccountId(chatId, chosen) });
}

/** What an awaiting chat's first send launches it as: a chat on an account, or a room. */
type FirstSendChoice = { kind: "account"; id: string } | { kind: "room" };

export function MessageInput(): m.Component<MessageInputAttrs> {
  let messageText = "";
  let currentChatId: string | null = null;
  let messageTextareaElement: HTMLTextAreaElement | null = null;
  // Whether the composer was disabled at the last render, so it takes focus the moment a provider makes it usable.
  let wasComposerDisabled = false;
  let messageRowElement: HTMLElement | null = null;
  // An edit refits the composer as typing, and so does the redraw that follows it: the browser scrolls the caret
  // into view after the input event, which can undo the edit's own scroll to the bottom.
  let isTypingUntilRedraw = false;
  // Refits the composer when its width changes, since that changes where the text wraps.
  let messageRowResizeObserver: ResizeObserver | null = null;
  let messageRowWidthPx = 0;
  // Set instead of sending when the user types one of the harness's declared
  // auth commands (the `open_auth` composer popup). Delivered raw, /login or
  // /logout would run the harness's own auth flow inside the agent's terminal
  // (or reach the model as prose), bypassing the managed agent-auth surface.
  let interceptedAuthCommand: string | null = null;
  // A message the chat declines to deliver: a slash command that would change the agent's terminal
  // rather than start a turn (it still works from that terminal, which the notice says), or a
  // /compact the server declined to run (nothing to compact yet, or the agent is busy or stopped).
  // Only a slash command offers Source view, where it can still be typed.
  let declinedSend: { title: string; body: string; isSourceViewOffered: boolean } | null = null;
  // Why the last send or interrupt failed, in the harness's own words, shown as a notice.
  // Component state like the notices above, NOT module state: every open chat panel mounts its
  // own MessageInput, and a module-level value would raise the notice in all of them at once.
  // `recovery` is what makes the notice actionable, and it is carried by the OPERATION rather
  // than the failure: a send can be repeated, so it offers Cancel / Retry / Restart, while a
  // failed interrupt has nothing to repeat and gets a plain OK. Any send failure qualifies --
  // a dialog holding the input, a readiness timeout, a transport error -- because the ways out
  // are the same whatever the reason; only the text differs.
  // `text` is what the user typed, for putting back in the composer; `sentText` is what was
  // actually sent (attachment references appended), for repeating the send. They differ whenever
  // the message carried attachments, and sending the typed text alone would silently drop them.
  type SendRecovery = {
    chatId: string;
    text: string;
    sentText: string;
    attachments: readonly ComposerAttachment[];
    /** The failed send's id: a retry goes under it, so the chat app answers it as it answered the
     *  first when the first was delivered after all, rather than delivering it twice. */
    messageId: string;
  };
  let actionFailure: FailureLines | null = null;
  // A failed Stop shares this notice, and telling the user their message could not be sent when
  // they clicked Stop is simply wrong copy.
  let actionFailureTitle = "Couldn't send your message";
  let actionFailureRecovery: SendRecovery | null = null;
  // The kind the server named for the refusal, when it named one (so the agent itself refused).
  let actionFailureServerKind: string | null = null;
  // A first send whose launch of its chat failed, for Try again to launch again.
  let actionFailureLaunch: SendRecovery | null = null;
  // How a sibling's failure is told again when its Try again fails too.
  let externalDescribe: ((error: unknown) => FailureLines) | null = null;
  let externalAction: NoticeAction | null = null;
  // The notice's own ways forward beyond Retry and Restart (Try again for a Stop, Start a new chat for a gone one).
  let actionFailureActions: NoticeAction[] = [];
  // Which recovery action is mid-flight, so the buttons disable rather than fire twice.
  let actionFailureInFlight: "retry" | "restart" | "ask" | null = null;
  // Why the notice's "Ask an agent to fix this" could not be sent, shown in the notice until it is asked again.
  let actionFailureAskFailure: ShownFailure | null = null;
  // One notice instance each: the component closes over its own dismiss handler, so sharing one
  // between notices would let whichever rendered last answer the others' Escape.
  const actionFailureNotice = makeNoticeDialog();
  const declinedCommandNotice = makeNoticeDialog();
  const authCommandNotice = makeNoticeDialog();
  // How the composer handles the failure (composerFailureKindOf), which decides which recoveries are worth offering.
  let actionFailureKind: ComposerFailureKind = "unknown";
  // A send mngr typed into the agent but could not confirm, or one the chat app never answered
  // (it may have delivered it before the answer was lost): its text went back to the composer
  // like any failed send's, but the agent may have it. Once its turn reaches the transcript
  // the copy comes back out and its notice closes, so the user does not send it a second time.
  // Kept past Cancel, which leaves the copy in the composer just the same. One per send, across
  // every chat this composer serves.
  let unconfirmedSends: { recovery: SendRecovery; priorTurnIds: ReadonlySet<string> }[] = [];

  function watchForUnconfirmedTurn(err: unknown, recovery: SendRecovery, priorTurnIds: ReadonlySet<string>): void {
    // A Send again that is unconfirmed too keeps the first watch: the turns read before the
    // first send let a landing of either one count.
    if (
      KINDS_THAT_MAY_HAVE_ARRIVED.has(composerFailureKindOf(err)) &&
      !unconfirmedSends.some((watched) => watched.recovery === recovery)
    ) {
      unconfirmedSends = [...unconfirmedSends, { recovery, priorTurnIds }];
    }
  }

  function stopWatchingUnconfirmedTurn(recovery: SendRecovery): void {
    unconfirmedSends = unconfirmedSends.filter((watched) => watched.recovery !== recovery);
  }
  // Whether the chat app refused the send because the chat's account was signed out: sending again cannot help, so
  // the notice offers choosing a provider instead of Retry and Restart.
  let isActionFailureOnSignedOutAccount = false;
  // Retry for a failure raised by a sibling view, which knows how to repeat its own operation.
  let externalRetry: (() => Promise<void>) | null = null;
  let fileInputElement: HTMLInputElement | null = null;
  let isInterruptInFlight = false;
  // The cancel of a running switch is out.
  let isCancelSwitchInFlight = false;
  // The chat whose switch-and-send message is still being prepared (uploads, the composer guard):
  // not sent yet, so the switch is still the next send's.
  let switchPreparingChatId: string | null = null;
  // The mention being typed, while the caret follows an ``@`` (section 7.2); null otherwise.
  let mentionDraft: MentionDraft | null = null;
  // Dictation (the workspace-voice-input spec), made on first use since it pulls the speech runtime in; its
  // state is mirrored here for the view, with the engine's own the truth for what a press does.
  let dictation: Dictation | null = null;
  let dictationState: DictationState = "idle";
  let dictationProblem: DictationProblem | null = null;
  // The stretch of the box the dictation owns, from the caret it started at; null while none runs.
  let dictationSpan: LiveSpan | null = null;
  // Keeps a phone's screen awake while a dictation runs or the speech model's load is shown: a locked screen
  // hides the page, which stops the words and takes the load's display down.
  let screenWakeLock: ScreenWakeLock | null = null;
  // Fills the mic's capsule, handed over by the button while its glyph is in the document.
  let setMeterLevel: ((level: number) => void) | null = null;
  // Send or Enter came while dictating: the words are finished first, then the box is sent (spec D8).
  let isSendPendingAfterDictation = false;
  // Whether the composer took input at the last render, for the shortcut, which arrives outside a render.
  let isComposerDisabledNow = false;
  // The page's speech model, which dictation needs loaded and every composer of the page shares: its load and
  // the load's progress as the page's cache holds them, observed for as long as the composer is on the page.
  let speechModelLoad: QueryObserver<SpeechModel, Error> | null = null;
  let speechModelProgress: QueryObserver<SpeechModelProgress | null, Error> | null = null;
  let stopObservingSpeechModel: (() => void) | null = null;
  let lastShownProgressPercent = -1;
  // Whether this composer is showing the model's load: from the press that asked for it until the load settles
  // or the user dismisses it. The load is the page's, and runs on either way.
  let isWatchingModelLoad = false;
  // When this composer last began showing the load, by ``Date.now``; null until it has.
  let modelLoadShownAtMs: number | null = null;
  // Whether the page has found out, since the composer mounted, what state the model is in, or has stopped
  // waiting to. Until then the mic is drawn as ready, so a model that is loaded never flashes the badge.
  let isSpeechModelStateKnown = false;
  // Whether the model's files are on this device: what the probe at mount said (null until it answers), and true
  // once the model has been seen loaded. While false a load is a download, sized by the probe.
  let isSpeechModelDownloaded: boolean | null = null;
  let speechModelDownloadBytes: number | null = null;

  /** Write the dictation's words into the box: the live span, with the caret at its end so the words stay in
   *  view, and only the final words to the saved draft. */
  function applyDictationSpan(): void {
    if (dictationSpan === null || currentChatId === null) return;
    const rendered = renderSpan(dictationSpan);
    messageText = rendered.text;
    if (rendered.draft === "") localStorage.removeItem(messageTextKey(currentChatId));
    else localStorage.setItem(messageTextKey(currentChatId), rendered.draft);
    if (messageTextareaElement !== null) {
      messageTextareaElement.value = rendered.text;
      messageTextareaElement.setSelectionRange(rendered.spanEnd, rendered.spanEnd);
      refitAfterEdit();
    }
    m.redraw();
  }

  /** Fix where the words will land: the caret (or the selection) as it is when dictation starts. */
  function openDictationSpan(): void {
    const textarea = messageTextareaElement;
    const start = textarea?.selectionStart ?? messageText.length;
    const end = textarea?.selectionEnd ?? messageText.length;
    dictationSpan = openSpan(messageText, start, end);
  }

  function ensureDictation(): Dictation {
    if (dictation !== null) return dictation;
    dictation = createDictation(dictationPageOptions(), {
      onStateChange: (state) => {
        dictationState = state;
        syncScreenWakeLock();
        // Once idle the words are ordinary text; the next dictation opens a span of its own.
        if (state === "idle") dictationSpan = null;
        if (state === "listening") readyChime.play();
        m.redraw();
      },
      onLevel: (level) => setMeterLevel?.(level),
      onFinalWords: (words) => {
        if (dictationSpan === null) return;
        dictationSpan = appendFinal(dictationSpan, words);
        applyDictationSpan();
        if (currentChatId !== null) {
          markDictatedSpeech(currentChatId);
          warmUpStoppedChat(currentChatId);
        }
      },
      onTentativeWords: (words) => {
        if (dictationSpan === null) return;
        dictationSpan = setTentative(dictationSpan, words);
        applyDictationSpan();
      },
      onUtteranceEnd: () => {
        if (dictationSpan === null) return;
        dictationSpan = clearTentative(dictationSpan);
        applyDictationSpan();
      },
      onProblem: (problem) => {
        dictationProblem = problem;
        m.redraw();
      },
    });
    return dictation;
  }

  function isSpeechModelLoaded(): boolean {
    return speechModelLoad?.getCurrentResult().status === "success";
  }

  /** Whether the model's load is drawn: this composer is watching it, and it is still running. */
  function isModelLoadShown(): boolean {
    return (
      isWatchingModelLoad && !isSpeechModelLoaded() && speechModelLoad?.getCurrentResult().fetchStatus === "fetching"
    );
  }

  /** What the mic shows: dictation's own state while one runs, else the speech model's as this composer sees it. */
  function micState(): DictationButtonState {
    if (dictationState !== "idle") return dictationState;
    if (isModelLoadShown()) return "loading";
    return isSpeechModelLoaded() || !isSpeechModelStateKnown ? "ready" : "load-needed";
  }

  /** The page knows what state the model is in, or has stopped waiting to. From here the mic is drawn as the
   *  page's cache has the model, so a worker that answers late with a loaded one still turns it ready. */
  function endSpeechModelStateWait(): void {
    if (isSpeechModelStateKnown) return;
    isSpeechModelStateKnown = true;
    m.redraw();
  }

  /** Follow the page's speech model from mount: observe its load and the load's progress in the page's cache,
   *  and find out what state it is in, on this device (the probe) and in the workspace's worker. */
  function observeSpeechModel(): void {
    const options = dictationPageOptions();
    const models = getSpeechModels();
    const queryClient = getVoiceQueryClient();
    // Not enabled: an observer that is would start the load by subscribing.
    speechModelLoad = new QueryObserver<SpeechModel, Error>(queryClient, {
      ...models.loadQueryOptions(options),
      enabled: false,
    });
    // The progress is written into the cache as the worker reports it, so its query has nothing to fetch with.
    speechModelProgress = new QueryObserver<SpeechModelProgress | null, Error>(queryClient, {
      queryKey: models.progressQueryKey(options),
      queryFn: skipToken,
    });
    const stopObservingLoad = speechModelLoad.subscribe((result) => {
      if (result.status === "success") {
        // A loaded model's files are on this device, whatever the probe said at mount.
        isSpeechModelDownloaded = true;
        // A failed load's notice has nothing left to ask for again: its Retry is the mic pressed, which dictates
        // once the model is loaded.
        if (dictationProblem !== null && isSpeechModelLoadProblem(dictationProblem)) dictationProblem = null;
      }
      m.redraw();
    });
    const stopObservingProgress = speechModelProgress.subscribe((result) => {
      // A download reports every chunk; the bar and the tooltip move by whole percents.
      const percent = result.data == null ? -1 : dictationProgressPercent(result.data);
      if (percent !== lastShownProgressPercent) {
        lastShownProgressPercent = percent;
        m.redraw();
      }
    });
    // A worker that neither greets nor fails never answers the discovery below.
    const stateWaitTimer = setTimeout(endSpeechModelStateWait, SPEECH_MODEL_STATE_WAIT_MS);
    stopObservingSpeechModel = () => {
      stopObservingLoad();
      stopObservingProgress();
      clearTimeout(stateWaitTimer);
    };
    probeEngineDownload(options)
      .then((probe) => {
        isSpeechModelDownloaded = probe.isDownloaded;
        speechModelDownloadBytes = probe.totalBytes;
        return models.discover(options, probe.isDownloaded);
      })
      .catch((error: unknown) => console.warn(`Could not tell what state the speech model is in: ${String(error)}`))
      .finally(() => {
        clearTimeout(stateWaitTimer);
        endSpeechModelStateWait();
      });
  }

  /** Keep the screen awake while this composer has something running in front of the user: a dictation, or a
   *  load of the speech model it is showing. A load dismissed to run on in the background holds nothing. */
  function syncScreenWakeLock(): void {
    screenWakeLock ??= createScreenWakeLock(browserWakeLockHost());
    screenWakeLock.setWanted(dictationState !== "idle" || isWatchingModelLoad);
  }

  /** The load this composer asked for has settled. One still shown ends as the press that asked for it meant:
   *  it goes on to dictate, with no second press, or says why the load failed. One dismissed since ends
   *  unremarked: nothing starts, and its failure is left for the press that asks again. */
  function handleModelLoadSettled(failure: DictationProblem | null): void {
    if (isWatchingModelLoad) {
      isWatchingModelLoad = false;
      if (failure !== null) dictationProblem = failure;
      else if (currentChatId !== null && !isComposerDisabledNow) beginDictation();
    }
    syncScreenWakeLock();
    m.redraw();
  }

  /** Ask the page for the speech model and show its load until it settles or is dismissed. A load already
   *  running is joined, and shown from where it has got to. */
  function watchModelLoad(): void {
    isWatchingModelLoad = true;
    modelLoadShownAtMs = Date.now();
    dictationProblem = null;
    getSpeechModels()
      .request(dictationPageOptions())
      .then(
        () => handleModelLoadSettled(null),
        (error: unknown) => handleModelLoadSettled(describeSpeechModelLoadError(error)),
      );
    syncScreenWakeLock();
    m.redraw();
  }

  /**
   * Stop showing the speech model's load, which calls off the dictation the load was to go on to. The load
   * itself runs on, since nothing cancels one, and the mic becomes ready by itself when it ends.
   *
   * This is the one place a shown load is dismissed: every trigger calls it, and a trigger added later should
   * too. The triggers: a press on the loading label, or on the mic or its shortcut while the load is shown
   * (``handleLoadingPress``); Escape from the page (``handleLoadingEscape``); text entered into the box (the
   * text box's ``oninput``; a press or a tap in the box alone dismisses nothing); the page going out of view
   * (``handlePageHidden``); and the composer turning to another chat, or leaving the page.
   */
  function dismissModelLoad(): void {
    if (!isWatchingModelLoad) return;
    isWatchingModelLoad = false;
    syncScreenWakeLock();
    m.redraw();
  }

  /** A press on what stands for the shown load, the label or the mic under it (which the shortcut presses),
   *  dismisses it. One right behind the press that asked for the load is that press landing twice, and does
   *  nothing; Escape and the page hiding are other gestures, and dismiss at any time. */
  function handleLoadingPress(): void {
    if (modelLoadShownAtMs !== null && Date.now() - modelLoadShownAtMs < REPEATED_PRESS_WINDOW_MS) return;
    dismissModelLoad();
    focusTextboxAfterDismissal();
  }

  /** A dismissal made on the load itself (the label, the shortcut, Escape) is the user turning back to typing,
   *  so on a desktop the text box takes the focus. A touch device's stays where it is: focusing the box there
   *  raises the keyboard, which a tap on the label did not ask for. */
  function focusTextboxAfterDismissal(): void {
    if (pageDeviceClass() === "desktop") focusMessageTextarea();
  }

  /** Begin a dictation whose words land where the caret is now. The speech model is loaded; a load this
   *  composer was showing for it is over. */
  function beginDictation(): void {
    isWatchingModelLoad = false;
    openDictationSpan();
    void ensureDictation().start();
  }

  /** The mic and the shortcut. While dictating, a press stops it. Otherwise a press on a shown load is the
   *  label's own (``handleLoadingPress``), a press on a loaded model dictates, and a press on one that is not
   *  loaded means the same with the load first: it asks for the model, shows the load, and goes on to dictate
   *  when the load ends, unless the load was dismissed meanwhile. */
  function handleMicPress(): void {
    if (currentChatId === null || isComposerDisabledNow) return;
    if (dictation !== null && dictation.state !== "idle") {
      if (dictation.state === "listening") void dictation.stop();
      return;
    }
    if (isModelLoadShown()) {
      handleLoadingPress();
      return;
    }
    // The press is the user gesture after which the browser lets the page sound.
    readyChime.wake();
    if (!isSpeechModelLoaded()) {
      watchModelLoad();
      return;
    }
    beginDictation();
  }

  /** Stop and keep the words heard so far; a dictation still starting has none, so it is called off. */
  function stopDictationKeepingWords(): void {
    if (dictation === null) return;
    if (dictation.state === "listening") void dictation.stop();
    else if (dictation.state === "starting") void dictation.cancel();
  }

  function disposeDictation(): void {
    dictation?.dispose();
    dictation = null;
    dictationState = "idle";
    dictationProblem = null;
    dictationSpan = null;
    isSendPendingAfterDictation = false;
    // Disposal reaches idle without a state change of its own.
    syncScreenWakeLock();
  }

  /** Escape while the model's load is shown dismisses it, from anywhere in the composer (the text box, or the
   *  label, which takes the focus the mic held) or from no control at all. A key meant for another control (a
   *  menu, a dialog), or one something in the composer has already answered, is left alone. */
  function handleLoadingEscape(event: KeyboardEvent): boolean {
    if (event.key !== "Escape" || event.defaultPrevented || !isModelLoadShown()) return false;
    const target = event.target;
    if (target == null) return false;
    const isFromPage = target === document || target === document.documentElement || target === document.body;
    const isFromComposer =
      typeof Element !== "undefined" && target instanceof Element && target.closest(".message-input") !== null;
    if (!isFromPage && !isFromComposer) return false;
    event.preventDefault();
    dismissModelLoad();
    focusTextboxAfterDismissal();
    return true;
  }

  function handleDictationShortcut(event: KeyboardEvent): void {
    if (handleLoadingEscape(event)) return;
    if (!keydownMatchesChords(event, [dictationShortcutChord(isAppleKeyboard())])) return;
    event.preventDefault();
    // A chord held down repeats: it stays the page's, and is one press.
    if (event.repeat) return;
    handleMicPress();
  }

  /** The page went out of view: its dictation stops, keeping the words, and a load it was showing is dismissed. */
  function handlePageHidden(): void {
    stopDictationKeepingWords();
    dismissModelLoad();
  }

  function handleVisibilityChange(): void {
    if (document.visibilityState === "hidden") handlePageHidden();
  }

  /** The shortcut works anywhere in the chat page, and the page going away takes the dictation with it: hidden
   *  keeps the words, closed drops the unfinished ones. The page's speech model is followed from here on. */
  function attachDictationToPage(): void {
    document.addEventListener("keydown", handleDictationShortcut);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pagehide", disposeDictation);
    mountedDictationHide = handlePageHidden;
    observeSpeechModel();
  }

  function detachDictationFromPage(): void {
    document.removeEventListener("keydown", handleDictationShortcut);
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    window.removeEventListener("pagehide", disposeDictation);
    if (mountedDictationHide === handlePageHidden) mountedDictationHide = null;
    stopObservingSpeechModel?.();
    stopObservingSpeechModel = null;
    speechModelLoad = null;
    speechModelProgress = null;
    dismissModelLoad();
    disposeDictation();
  }

  function mentionRows(): MentionHandle[] {
    return mentionDraft === null ? [] : matchingHandles(mentionDraft.query).slice(0, MENTION_ROW_CAP);
  }

  /** Read the mention under the caret after the text changed or the caret moved; a fresh ``@`` asks for the table.
   *  Only in a room (section 7.2): in an agent chat a handle is a word the agent reads like any other. */
  function updateMentionDraft(textarea: HTMLTextAreaElement): void {
    if (currentChatId === null || !isRoomChat(currentChatId)) {
      mentionDraft = null;
      return;
    }
    const previous = mentionDraft;
    mentionDraft = mentionDraftAt(textarea.value, textarea.selectionStart ?? textarea.value.length, previous);
    if (mentionDraft !== null && previous === null) void ensureMentionHandles().then(() => m.redraw());
  }

  function pickMention(handle: MentionHandle): void {
    if (mentionDraft === null || messageTextareaElement === null || currentChatId === null) return;
    const inserted = insertMention(messageText, mentionDraft, handle.handle);
    messageText = inserted.text;
    messageTextareaElement.value = inserted.text;
    messageTextareaElement.setSelectionRange(inserted.caret, inserted.caret);
    localStorage.setItem(messageTextKey(currentChatId), messageText);
    mentionDraft = null;
    refitAfterEdit();
    focusMessageTextarea();
    m.redraw();
  }

  /** The popover's keys while a mention is being typed; whether the key was taken. */
  function handleMentionKeydown(event: KeyboardEvent): boolean {
    if (mentionDraft === null) return false;
    const rows = mentionRows();
    if (event.key === "Escape") {
      mentionDraft = null;
      return true;
    }
    if (rows.length === 0) return false;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const step = event.key === "ArrowDown" ? 1 : -1;
      mentionDraft = { ...mentionDraft, index: (mentionDraft.index + step + rows.length) % rows.length };
      return true;
    }
    if (event.key === "Enter" || event.key === "Tab") {
      pickMention(rows[Math.min(mentionDraft.index, rows.length - 1)]);
      return true;
    }
    return false;
  }

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
  }

  function refitComposer(isTyping = false): void {
    if (messageRowElement !== null && messageTextareaElement !== null) {
      fitComposer(messageRowElement, messageTextareaElement, isTyping);
    }
  }

  function refitAfterEdit(): void {
    isTypingUntilRedraw = true;
    refitComposer(true);
  }

  function attachMessageRow(rowVnode: m.VnodeDOM): void {
    const rowElement = rowVnode.dom as HTMLElement;
    messageRowElement = rowElement;
    messageRowWidthPx = rowElement.clientWidth;
    messageRowResizeObserver = new ResizeObserver(() => {
      if (rowElement.clientWidth !== messageRowWidthPx) {
        messageRowWidthPx = rowElement.clientWidth;
        refitComposer();
      }
    });
    messageRowResizeObserver.observe(rowElement);
    refitComposer();
  }

  function detachMessageRow(): void {
    messageRowResizeObserver?.disconnect();
    messageRowResizeObserver = null;
    messageRowElement = null;
  }

  /**
   * One attachment chip, 40px tall: an image is its thumbnail alone; anything else is an icon beside
   * its name and its size, or what it points at for a file the chat made for the user (an element
   * reference). While uploading, a spinner sits over the thumbnail on a dark wash, or where the icon
   * would be. The remove button rides the top-right corner, shown on hover.
   */
  function renderComposerAttachment(chatId: string, attachment: ComposerAttachment): m.Vnode {
    const isUploading = attachment.status === "uploading";
    const isError = attachment.status === "error";
    // The local pixels while uploading, then the uploaded copy.
    const thumbnailUrl = attachment.previewUrl ?? attachment.uploaded?.url;
    const isThumbnail = attachment.isImage && !isError && thumbnailUrl !== undefined;
    const size = attachment.uploaded?.size ?? attachment.localSize;
    const spinner = m.trust(spinnerIcon(16));
    const isDescribed = attachment.summary !== undefined;
    const remove = isUploading
      ? null
      : m(
          "button",
          {
            type: "button",
            // z-10: above the strip's end fades, so a half-hidden chip's remove button stays usable.
            // Centred on the corner inside the chip's 1px border, not on the border's outer edge.
            class:
              "composer-attachment-remove absolute -top-[9px] -right-[9px] z-10 inline-flex h-5 w-5 cursor-pointer " +
              "items-center justify-center rounded-full bg-inverse text-on-accent opacity-0 shadow-sm " +
              "transition-opacity group-hover/chip:opacity-100 focus-visible:opacity-100",
            "aria-label": "Remove attachment",
            ...hoverTooltipAttrs("Remove attachment", "above"),
            onclick: () => removeComposerAttachment(chatId, attachment.localId),
          },
          m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
        );
    // The status modifier is an interpolated marker; the one status the look distinguishes (a
    // failed upload's red border) rides beside it.
    const chipMarker = `composer-attachment composer-attachment--${attachment.status}`;
    if (isThumbnail) {
      return m(
        "div",
        {
          key: attachment.localId,
          class: `${chipMarker} group/chip relative flex-none`,
        },
        [
          // The image fills the whole 40px; its border is drawn over it rather than taking its edge.
          m("img", {
            class: `composer-attachment-thumb block ${ATTACHMENT_CHIP_HEIGHT_CLASS} w-10 rounded-md object-cover`,
            src: thumbnailUrl,
            alt: attachment.fileName,
          }),
          m("span", { class: "pointer-events-none absolute inset-0 rounded-md border border-subtle" }),
          isUploading
            ? m(
                "span",
                {
                  class:
                    "composer-attachment-uploading absolute inset-0 flex items-center justify-center rounded-md " +
                    "bg-black/40 text-white",
                  "aria-label": "Uploading",
                  role: "img",
                },
                spinner,
              )
            : null,
          remove,
        ],
      );
    }
    const summary = attachment.summary ?? (size === undefined ? undefined : formatFileSize(size));
    const detail = isError
      ? m(
          "span",
          { class: `${ATTACHMENT_DETAIL_CLASS} composer-attachment-detail--error text-danger` },
          "Upload failed",
        )
      : summary === undefined
        ? null
        : m("span", { class: `${ATTACHMENT_DETAIL_CLASS} composer-attachment-summary text-secondary` }, summary);
    return m(
      "div",
      {
        key: attachment.localId,
        class:
          `${chipMarker} group/chip relative flex ${ATTACHMENT_CHIP_HEIGHT_CLASS} max-w-[160px] flex-none items-center ` +
          "gap-1 rounded-md border bg-surface-secondary pr-1.5 pl-1 " +
          (isError ? "border-danger-border" : "border-subtle"),
      },
      [
        m(
          "span",
          {
            class: "composer-attachment-icon inline-flex h-4 w-4 flex-none items-center justify-center text-secondary",
          },
          isUploading
            ? m("span", { class: "composer-attachment-uploading", "aria-label": "Uploading", role: "img" }, spinner)
            : m.trust(icon(isDescribed ? "pointer" : "file", { size: 16, strokeWidth: 1.8 })),
        ),
        m("span", { class: "composer-attachment-info flex min-w-0 flex-col" }, [
          m(
            "span",
            {
              class: "composer-attachment-name truncate text-(length:--font-size-helper) leading-[1.3] text-primary",
            },
            attachment.fileName,
          ),
          detail,
        ]),
        remove,
      ],
    );
  }

  return {
    view(vnode) {
      const chatId = vnode.attrs.chatId;

      if (!chatId) {
        return null;
      }

      if (currentChatId !== chatId) {
        // A dictation belongs to the chat it started in, and so does the showing of a load asked for from it.
        disposeDictation();
        dismissModelLoad();
        currentChatId = chatId;
        messageText = localStorage.getItem(messageTextKey(chatId)) ?? "";
        const persistedHint = localStorage.getItem(composerHintKey(chatId));
        if (persistedHint !== null && !composerHints.has(chatId)) {
          // Set by the chat root before this page loaded: taken up as a live hint would be, focusing once.
          composerHints.set(chatId, persistedHint);
          pendingComposerFocuses.add(chatId);
        }
        isInterruptInFlight = false;
        isCancelSwitchInFlight = false;
        // The notices name a command typed for the previous agent, so they must not follow the
        // user to the next one.
        declinedSend = null;
        interceptedAuthCommand = null;
        // Safe to drop: the failed message was put back in that agent's composer when the send
        // failed, so nothing is lost by closing its notice unanswered.
        clearActionFailureNotice();
      }

      // A sibling view (a native tap whose resend failed) merged a returned block into this agent's
      // persisted draft; adopt it into the live composer so it is visible at once, then clear the flag.
      // A sibling view raised a failure for this agent; adopt it into the notice.
      const pendingNotice = pendingFailureNotices.get(chatId);
      if (pendingNotice !== undefined) {
        pendingFailureNotices.delete(chatId);
        clearActionFailureNotice();
        actionFailureTitle = pendingNotice.title;
        actionFailure = pendingNotice.describe(pendingNotice.error);
        actionFailureKind = pendingNotice.kind ?? "unknown";
        externalRetry = pendingNotice.retry ?? null;
        externalDescribe = pendingNotice.describe;
        externalAction = pendingNotice.action ?? null;
      }

      const pendingPrepend = pendingComposerPrepends.get(chatId);
      // The clear before the prepend: a take-then-hand-straight-back (a new chat the shell could
      // not start) leaves both pending on the same pass, and the restored draft must win.
      if (pendingComposerClears.delete(chatId)) {
        messageText = "";
      }
      if (pendingPrepend !== undefined) {
        pendingComposerPrepends.delete(chatId);
        messageText = pendingPrepend;
      }
      if (pendingComposerFocuses.delete(chatId)) {
        // After this pass has drawn the textarea the hint is for.
        queueMicrotask(focusMessageTextarea);
      }
      const composerHint = composerHints.get(chatId) ?? null;

      // After the hand-back above is adopted: the failed send's copy arrives through it.
      for (const { recovery, priorTurnIds } of unconfirmedSends) {
        if (!hasUserTurnLandedSince(recovery.chatId, recovery.sentText, priorTurnIds)) {
          continue;
        }
        stopWatchingUnconfirmedTurn(recovery);
        clearRestoredMessage(recovery.chatId, recovery.text, recovery.attachments);
        // Only its own notice, and not out from under a Send again already on its way.
        if (actionFailureRecovery === recovery && actionFailureInFlight === null) {
          clearActionFailureNotice();
        }
      }

      // The box's text is settled for this pass, and an emptied box holds no dictated words, whatever emptied
      // it: the user's own deleting, a command the composer acted on, a delivered copy taken out just above.
      // The saved draft is asked as well, since another page showing this chat has a box of its own, and
      // its empty one says nothing about a draft dictation wrote into here.
      if (messageText === "" && !localStorage.getItem(messageTextKey(chatId))) {
        forgetDictatedSpeech(chatId);
      }

      /** What a send (or a switch) puts on the wire, with what to put back if it fails. */
      interface PreparedSend {
        /** What the user typed, for the bubble and for restoring the composer. */
        text: string;
        /** What goes to the agent: the text, the dictation note when the draft may hold dictated words, and
         *  the attachment references appended. */
        finalText: string;
        attachments: readonly ComposerAttachment[];
        /** Whether dictation wrote into the draft, so a message put back counts as holding dictated words again. */
        mayContainDictatedSpeech: boolean;
      }

      /**
       * The preflight every send shares: the harness's composer guard, the upload wait, the
       * failed-upload refusal, and the empty check; then the composer is cleared and what was
       * in it comes back for the wire. Null when nothing is to be sent (the guard raised a notice,
       * an upload failed, or the box was empty).
       */
      async function prepareSend(): Promise<PreparedSend | null> {
        if (!chatId) {
          return null;
        }
        // The composer guard is whatever the agent's harness declared (its
        // `composer_command` popups on the catalog): auth commands open the
        // harness's agent-auth surface, declined commands get the notice, and a
        // harness that declared nothing (pi's declines) sends everything as-is.
        // Only slash-shaped messages consult it, and only those block on the
        // catalog when it has not loaded yet -- otherwise an early /login could
        // slip through the fetch window.
        const guardedText = withoutNulCharacters(messageText);
        if (guardedText.trim().startsWith("/")) {
          const harness = getChatById(chatId)?.active_agent.harness;
          if (getHarnessCatalog(harness) === null) {
            await ensureHarnessCatalogs();
          }
          // ``/fast on`` and ``/fast off`` choose the chat's fast mode (on a harness that has
          // one) rather than reaching the harness or its notice: the mode is the chat's setting.
          const fastMode = parseFastModeCommand(guardedText);
          if (fastMode !== null && hasFastModeLimit(harness)) {
            chooseFastMode(chatId, fastMode, getEventsForChat(chatId));
            messageText = "";
            localStorage.removeItem(messageTextKey(chatId));
            m.redraw();
            return null;
          }
          const match = findComposerPopup(harness, guardedText);
          if (match !== null) {
            if (match.popup.action === "open_auth") {
              interceptedAuthCommand = match.command;
            } else {
              declinedSend = {
                title: `${match.command} can't be sent from chat`,
                body:
                  match.popup.notice_body ??
                  "Chat doesn't take this command. Say what you want in your own words instead, and the agent will do it.",
                isSourceViewOffered: true,
              };
            }
            m.redraw();
            return null;
          }
        }
        // Wait for in-flight uploads so a just-dropped file is included rather
        // than dropped from the message.
        await waitForComposerUploads(chatId);

        const attachmentPaths = getReadyAttachmentPaths(chatId);
        const text = withoutNulCharacters(messageText);

        // An upload that failed is dropped by getReadyAttachmentPaths and its chip is cleared
        // below, so without this the file would leave the message silently and the only clue
        // would be a small label that then disappears. Refuse the send and say which file.
        const failedAttachments = getComposerAttachments(chatId).filter((attachment) => attachment.status === "error");
        if (failedAttachments.length > 0) {
          // Same guard as the send-failure path: the upload wait above is awaited, so the user
          // may have switched agents, and a notice about this agent's files must not land on
          // another agent's chat.
          if (currentChatId !== chatId) {
            return null;
          }
          const names = failedAttachments.map((attachment) => attachment.fileName).join(", ");
          clearActionFailureNotice();
          actionFailureTitle =
            failedAttachments.length === 1 ? "An attachment didn't upload" : "Some attachments didn't upload";
          actionFailure = {
            body: [
              `${names} could not be uploaded, so the message was not sent. Remove the attachment, or try again.`,
            ],
            details: null,
            isReportable: false,
          };
          m.redraw();
          return null;
        }

        if (!text.trim() && attachmentPaths.length === 0) {
          // Nothing to send. Reachable by clicking Send with an empty box, which needs no
          // explanation -- the button simply does nothing.
          return null;
        }

        // The note goes before the attachment block, which is only recognised at the very end of a message.
        const mayContainDictatedSpeech = hasDictatedSpeech(chatId);
        const finalText = buildMessageWithAttachments(
          mayContainDictatedSpeech ? withDictationNote(text) : text,
          attachmentPaths,
        );
        // Snapshot for rollback if the send fails.
        const attachments = getComposerAttachments(chatId);

        messageText = "";
        clearComposerAttachments(chatId);
        localStorage.removeItem(messageTextKey(chatId));
        forgetDictatedSpeech(chatId);
        return { text, finalText, attachments, mayContainDictatedSpeech };
      }

      async function handleSend(): Promise<void> {
        if (!chatId) {
          return;
        }
        const prepared = await prepareSend();
        if (prepared === null) {
          return;
        }
        // The hint asked a question; the send answered it.
        clearComposerHint(chatId);
        const { text: sentText, finalText, attachments: sentAttachments, mayContainDictatedSpeech } = prepared;

        // Paint an optimistic "Sending…" bubble at the tail immediately -- the ONE
        // optimism the frontend is allowed (contract A2). It is a client-only overlay
        // (see models/OutgoingMessages) whose removal is BACKEND-DRIVEN: it drops only
        // once the real message arrives from the backend (its queued chip or committed
        // transcript turn), real-first, so there is never a gap. The id minted here rides
        // the send too, so a chat app that holds the message while the chat switches
        // harness can name it back and the bubble stands down for that rendering.
        const messageId = mintMessageId();
        const outgoingId = addOutgoing(chatId, finalText, messageId);
        m.redraw();
        let priorTurnIds: ReadonlySet<string> = new Set();

        try {
          // A chat awaiting its first send (a seeded one, one an intake minted, or the one an empty
          // chat list opens on) has no agent yet, and this send is what launches one: on the account
          // it was minted for, else the signed-in one, else the one the chooser produces. A seeded
          // chat's message rides the launch as the agent's first, wrapped in the seed; any other is
          // sent once the launch is under way, held like any send to a starting chat, so Stop can
          // take it back until the agent is up. A chat whose launch is already on its way (the
          // sign-in's) is a creating chat to this send.
          const awaiting = getProvisionalChat(chatId);
          if (
            awaiting?.phase === "awaiting_first_send" &&
            getChatById(chatId) === undefined &&
            !isLaunchInFlight(chatId)
          ) {
            const choice = await chooseAccountForFirstSend(awaiting);
            if (choice === null) {
              // The chooser closed with no sign-in: nothing was launched, and the message is
              // the user's to keep.
              dropOutgoing(chatId, outgoingId);
              restoreFailedMessageToComposer(chatId, sentText, sentAttachments, mayContainDictatedSpeech);
              m.redraw();
              return;
            }
            try {
              await launchForFirstSend(chatId, choice, awaiting.is_seeded, finalText);
            } catch (err) {
              // Refused or unanswered before any agent existed: the message is the user's to keep, and Try again
              // launches the chat again rather than sending to a chat no launch was made for.
              console.error(`The launch of chat ${chatId} was refused: ${describeRequestError(err)}`);
              dropOutgoing(chatId, outgoingId);
              restoreFailedMessageToComposer(chatId, sentText, sentAttachments, mayContainDictatedSpeech);
              if (currentChatId === chatId) {
                clearActionFailureNotice();
                actionFailure = describeLaunchFailure(err);
                actionFailureLaunch = {
                  chatId,
                  text: sentText,
                  sentText: finalText,
                  attachments: sentAttachments,
                  messageId,
                };
              }
              m.redraw();
              return;
            }
            if (choice.kind === "account" && !awaiting.is_seeded) {
              priorTurnIds = sentUserTurnIds(chatId);
              noteSendToChatNotYetAnAgent(chatId, messageId, {
                text: sentText,
                attachments: sentAttachments,
                outgoingId,
                isInBox: false,
              });
              const firstStatus = await sendMessage(chatId, finalText, messageId);
              dropOutgoingNoTurnWillRetire(chatId, outgoingId, firstStatus);
              noteOutgoingAccepted(chatId, outgoingId);
              settleSendToChatNotYetAnAgent(chatId, messageId, firstStatus);
              refocusAfterSend();
              return;
            }
            try {
              await whenChatRegistered(chatId);
            } catch (err) {
              // The create failed after the launch took the message: the chat keeps it as its
              // first message and the page's "Try again" delivers it, so it does not go back to
              // the composer (a copy there would be sent twice). The page shows the reason.
              console.error(`The seeded chat ${chatId} could not be started: ${describeRequestError(err)}`);
              dropOutgoing(chatId, outgoingId);
              m.redraw();
              return;
            }
            refocusAfterSend();
            return;
          }
          // A chat still being created has no agent to deliver to yet: the chat app holds the send
          // and delivers it once the create lands, so a page closed meanwhile loses nothing, and the
          // bubble stays until the turn arrives. A create that has already failed takes no sends,
          // and the message goes back to the composer like any failed send.
          // A launch this page posted that the backend has not yet reported creating may not hold
          // the send yet, so that one send waits for the chat to land.
          if (isLaunchInFlight(chatId)) await whenChatRegistered(chatId);
          refuseSendToFailedChat(chatId);
          priorTurnIds = sentUserTurnIds(chatId);
          noteSendToChatNotYetAnAgent(chatId, messageId, {
            text: sentText,
            attachments: sentAttachments,
            outgoingId,
            isInBox: false,
          });
          const status = await sendMessage(chatId, finalText, messageId);
          // The send resolved: the message is now real (committed, queued, or held for the
          // agent the chat is switching to), so its "Sending…" bubble is removed by the arriving
          // transcript turn, queued snapshot, or held-send snapshot (see OutgoingMessages).
          dropOutgoingNoTurnWillRetire(chatId, outgoingId, status);
          noteOutgoingAccepted(chatId, outgoingId);
          settleSendToChatNotYetAnAgent(chatId, messageId, status);
        } catch (err) {
          if (settleSendToChatNotYetAnAgent(chatId, messageId, null)) {
            // Its chat's discard already put it back in the box and said why.
            dropOutgoing(chatId, outgoingId);
            m.redraw();
            refocusAfterSend();
            return;
          }
          // A refusal means the message was NOT accepted; one nothing answered, or mngr could not
          // confirm, may have reached the agent anyway (KINDS_THAT_MAY_HAVE_ARRIVED). A /compact the server declined
          // to run gets the declined-command notice rather than the failure notice, since retrying
          // at once cannot change it.
          const isDeclined = isDeclinedCompact(err);
          if (!isDeclined) {
            console.error(`Failed to send message to chat ${chatId}: ${describeRequestError(err)}`);
          }
          dropOutgoing(chatId, outgoingId);
          // Back in the composer immediately: the recovery record is closure state, so a reload
          // would take the message with it (contract A1a). A repeat send removes that copy once
          // it has landed.
          restoreFailedMessageToComposer(chatId, sentText, sentAttachments, mayContainDictatedSpeech);
          const recovery = { chatId, text: sentText, sentText: finalText, attachments: sentAttachments, messageId };
          watchForUnconfirmedTurn(err, recovery, priorTurnIds);
          catchUpOnSignedOutAccount(err);
          // Notices only if they are still on the agent that failed -- this catch runs after an
          // await, so they may have switched and the switch-clear has already gone by.
          if (currentChatId === chatId) {
            if (isDeclined) {
              declinedSend = {
                title: DECLINED_COMPACT_TITLE,
                body: declinedCompactSentence(err),
                isSourceViewOffered: false,
              };
            } else {
              clearActionFailureNotice();
              noteSendFailure(err);
              actionFailureRecovery = recovery;
            }
          }
          m.redraw();
        }

        refocusAfterSend();
      }

      /**
       * What a chat's first send launches it as: the account the chat was minted for when it
       * names one (an intake's), else the signed-in one when there is one, else whatever the
       * provider chooser produces; a room when the chooser's room row was picked instead
       * (section 7.3), or null when the chooser closed with neither.
       */
      async function chooseAccountForFirstSend(provisional: ProvisionalChat): Promise<FirstSendChoice | null> {
        await whenAccountsReadyToChoose();
        const account = accountForFirstSend(provisional.account_id);
        if (account !== null) {
          return { kind: "account", id: account.id };
        }
        return new Promise((resolve) => {
          openProviderChooser({
            onSignedIn: (accountId) => resolve({ kind: "account", id: accountId }),
            onDismissed: () => resolve(null),
            onRoomChosen: () => resolve({ kind: "room" }),
          });
        });
      }

      /**
       * The armed switch, carried out (spec 5.1): the typed message becomes the new agent's first,
       * through the handoff route, with the model picked in the dialog. The choice was made in the
       * dialog, so nothing asks again here. The bubble painted here carries the send-time id, so
       * the held-send rendering the next snapshot brings replaces it exactly; the queued text
       * draining took off the old agent comes back for the composer.
       */
      async function handleSwitchAndSend(): Promise<void> {
        if (!chatId || isSwitchSending(chatId) || switchPreparingChatId === chatId) {
          return;
        }
        switchPreparingChatId = chatId;
        let prepared: PreparedSend | null;
        try {
          prepared = await prepareSend();
        } finally {
          if (switchPreparingChatId === chatId) switchPreparingChatId = null;
        }
        if (prepared === null) {
          m.redraw();
          return;
        }
        const { text: sentText, finalText, attachments: sentAttachments, mayContainDictatedSpeech } = prepared;
        // Read once the message is ready, not at the press: until then the choice could still be
        // changed or taken back, and the account and its pick must go out as one.
        const target = nextSendSwitchTarget(chatId);
        if (target === null) {
          restoreFailedMessageToComposer(chatId, sentText, sentAttachments, mayContainDictatedSpeech);
          m.redraw();
          return;
        }
        const pick = getPendingPick(chatId)?.identity ?? null;
        setSwitchSending(chatId, true);
        const messageId = mintMessageId();
        const outgoingId = addOutgoing(chatId, finalText, messageId);
        m.redraw();
        try {
          await whenChatRegistered(chatId);
          const { returned_block } = await switchChat(chatId, target.id, finalText, messageId, pick);
          prependToComposer(chatId, returned_block);
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to switch chat ${chatId} to ${target.id}: ${detail}`);
          dropOutgoing(chatId, outgoingId);
          restoreFailedMessageToComposer(chatId, sentText, sentAttachments, mayContainDictatedSpeech);
          if (currentChatId === chatId) {
            clearActionFailureNotice();
            actionFailureTitle = `Couldn't switch to ${harnessLabel(target.harness)}`;
            actionFailure = describeSwitchFailure(err, harnessLabel(target.harness), true);
            if (isRefusedForNewChat(err)) {
              actionFailureActions = [
                {
                  label: "Start a new chat",
                  run: () => {
                    clearActionFailureNotice();
                    m.redraw();
                    void startNewChatWithDraft(chatId, target.id, pick);
                  },
                },
              ];
            } else if (describeRequestFailure(err).cause !== "not_answered") {
              actionFailureActions = [tryAgainAction(() => void handleSwitchAndSend())];
            }
          }
        } finally {
          setSwitchSending(chatId, false);
          m.redraw();
        }
        refocusAfterSend();
      }

      /** Call a running switch off (spec 5.6): the confirming message comes back to the composer, and
       *  the pending lane stays so the user can try again. */
      async function handleCancelSwitch(): Promise<void> {
        if (!chatId || isCancelSwitchInFlight) {
          return;
        }
        isCancelSwitchInFlight = true;
        m.redraw();
        try {
          const { returned_block } = await cancelHandoff(chatId);
          prependToComposer(chatId, returned_block);
        } catch (err) {
          console.error(`Failed to cancel the switch of chat ${chatId}: ${describeRequestError(err)}`);
          clearActionFailureNotice();
          if (isNothingToCancel(err)) {
            // Nothing failed: the switch finished or was called off before the press arrived.
            actionFailureTitle = "Nothing to cancel";
            actionFailure = {
              body: [failureSentence(describeRequestFailure(err)).message],
              details: null,
              isReportable: false,
            };
          } else {
            actionFailureTitle = "Couldn't cancel the switch";
            actionFailure = describeCancelSwitchFailure(err);
            actionFailureActions = [tryAgainAction(() => void handleCancelSwitch())];
          }
        } finally {
          isCancelSwitchInFlight = false;
          m.redraw();
        }
      }

      function refocusAfterSend(): void {
        requestAnimationFrame(() => {
          // Not while a notice is open: this rAF lands after mithril has mounted the notice and
          // focused its OK button, so refocusing the composer would steal it -- leaving a modal
          // the keyboard cannot dismiss, and an Enter that re-sends the just-restored text.
          if (actionFailure === null && declinedSend === null) {
            focusMessageTextarea();
          }
        });
      }

      async function handleStopToComposer(): Promise<void> {
        if (!chatId || isInterruptInFlight) {
          return;
        }
        // Hide the stop button until the request settles so the user cannot fire
        // off multiple restarts in quick succession.
        isInterruptInFlight = true;
        // Snapshot the Sending bubbles that exist BEFORE the interrupt round-trip. On
        // success we clear exactly these: every one is now either Delivered (its turn
        // already dropped its bubble via arrival) or Returned into the composer via the
        // block below -- so any that remain are Returned with no arrival to clear them
        // (the ghost). Clearing only the pre-interrupt set leaves a message the user
        // sends DURING the round-trip untouched (it is not in the returned block).
        const preInterruptBubbleIds = getOutgoingMessages(chatId).map((message) => message.id);
        m.redraw();
        try {
          // Interrupt the agent and pull any queued messages back into the composer,
          // unsent, for the user to edit and send. Empty block = nothing was queued
          // (a clean no-op).
          const { block } = await drainToComposer(chatId);
          // Every not-Delivered message is now back in the composer (or was Delivered and
          // dropped its own bubble); clear the pre-interrupt Sending bubbles so none ghost.
          clearOutgoing(chatId, preInterruptBubbleIds);
          if (block) {
            // Merge instead of drop: prepend the handed-back block above any existing draft
            // (block, blank line, draft) rather than dropping it when the composer is
            // non-empty. Under pi's native retract the messages survive nowhere else, so
            // dropping them here would lose them outright.
            // The dictation notes the queued messages carried are for the model, not the box: the
            // draft counts as holding dictated words instead, so its next send carries one note.
            const returned = withoutDictationNotes(block);
            const draft = messageText;
            const merged = draft.trim().length === 0 ? returned.text : `${returned.text}\n\n${draft}`;
            messageText = merged;
            localStorage.setItem(messageTextKey(chatId), merged);
            if (returned.hadNote) markDictatedSpeech(chatId);
          }
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to interrupt chat ${chatId}: ${detail}`);
          // Surface the failure: they deliberately clicked Stop, and on failure
          // the agent is still running. Same notice as a failed send -- leaving this one as a
          // system alert while its neighbour is a styled notice is worse than either.
          // Never inherit a previous send's recovery: an interrupt has nothing to repeat, and
          // leaving one attached would offer Retry bound to an unrelated message.
          clearActionFailureNotice();
          actionFailureTitle = "Couldn't stop the agent";
          actionFailure = describeStopFailure(err);
          actionFailureActions = [tryAgainAction(() => void handleStopToComposer())];
        } finally {
          isInterruptInFlight = false;
          m.redraw();
        }
      }

      /** Enter and the send button do the same thing: send, or carry the armed switch out. */
      function handleSubmit(): Promise<void> {
        if (chatId && isSwitchSending(chatId)) {
          return Promise.resolve();
        }
        if (dictation !== null && dictation.state !== "idle") {
          // One action sends what was said: the words are finished first, and the send follows (spec D8).
          isSendPendingAfterDictation = true;
          stopDictationKeepingWords();
          m.redraw();
          return Promise.resolve();
        }
        if (switchTarget !== null) {
          return chatId && canSend ? handleSwitchAndSend() : Promise.resolve();
        }
        return handleSend();
      }

      function handleKeydown(event: KeyboardEvent): void {
        // Escape while dictating drops the words that are not yet final; the final ones stay (spec D5).
        if (dictation !== null && dictation.state !== "idle" && event.key === "Escape") {
          event.preventDefault();
          void dictation.cancel();
          return;
        }
        if (handleMentionKeydown(event)) {
          event.preventDefault();
          m.redraw();
          return;
        }
        // The fallback is the manifest's defaults, so sending works in the moment
        // before the resolved chords land; Shift+Enter stays the fixed line break.
        if (matchesKeybinding(event, "send-message", ["enter", "ctrl+enter", "meta+enter"])) {
          event.preventDefault();
          void handleSubmit();
        }
      }

      function handlePaste(event: ClipboardEvent): void {
        if (!chatId) {
          return;
        }
        const files = imageFilesFromClipboard(event.clipboardData);
        if (files.length > 0) {
          event.preventDefault();
          uploadFilesToComposer(chatId, files);
        }
      }

      function openFilePicker(): void {
        fileInputElement?.click();
      }

      function dismissAuthCommandNotice(): void {
        interceptedAuthCommand = null;
        messageText = "";
        if (chatId) {
          localStorage.removeItem(messageTextKey(chatId));
        }
        m.redraw();
      }

      function dismissDeclinedCommandNotice(): void {
        declinedSend = null;
        m.redraw();
      }

      /** Put a failed message back in the composer, in FRONT of whatever is already there; one that may
       *  hold dictated words makes the draft it joins one that may. */
      function restoreFailedMessageToComposer(
        forChatId: string,
        text: string,
        attachments: readonly ComposerAttachment[],
        mayContainDictatedSpeech: boolean,
      ): void {
        // Reuses the module-level hand-back that Stop's drain, QueuedMessageView and the switch
        // dialog already go through, rather than inventing a second set of rules for the same job.
        restoreToComposer(forChatId, text, attachments);
        if (mayContainDictatedSpeech) markDictatedSpeech(forChatId);
        if (currentChatId === forChatId) {
          messageText = localStorage.getItem(messageTextKey(forChatId)) ?? text;
        }
      }

      /** Drop the first occurrence of ``block`` from ``text``, tidying the separator it left. */
      function removeFirstBlock(text: string, block: string): string {
        if (!block) {
          // An attachments-only message has no text to remove, and indexOf("") matches at 0 --
          // which would reformat a draft this never touched.
          return text;
        }
        const at = text.indexOf(block);
        if (at === -1) {
          return text;
        }
        const before = text.slice(0, at);
        const after = text.slice(at + block.length);
        return `${before}${after}`
          .replace(/^\n+/, "")
          .replace(/\n{3,}/g, "\n\n")
          .trimEnd();
      }

      /** Remove just the restored copy once a repeat send (or an unconfirmed one's turn) has landed, leaving the rest alone. */
      function clearRestoredMessage(
        forChatId: string,
        restoredText: string,
        deliveredAttachments: readonly ComposerAttachment[],
      ): void {
        const deliveredIds = new Set(deliveredAttachments.map((attachment) => attachment.localId));
        // Emphatically NOT "clear the composer". By this point the box can also hold a draft
        // typed while the send was failing, and -- after Restart -- the queue block drained out of
        // the harness so the restart would not destroy it. Wiping it wholesale would throw away
        // the very messages this feature exists to protect. Remove the one copy that was just
        // delivered, and leave everything else exactly where it is.
        // Removed wherever it sits, not just at the front: Restart drains the harness queue back
        // into the composer BEFORE sending, so by now the delivered message usually has that
        // block above it and a prefix-only strip would leave it behind -- sent, and still in the
        // box. Only the first occurrence goes, so a user who genuinely typed the same text twice
        // keeps their copy.
        const current = localStorage.getItem(messageTextKey(forChatId)) ?? "";
        const withoutRestored = removeFirstBlock(current, restoredText);
        if (withoutRestored) {
          localStorage.setItem(messageTextKey(forChatId), withoutRestored);
        } else {
          localStorage.removeItem(messageTextKey(forChatId));
        }
        // A hand-back the chat's composer has not taken in yet (the chat is not on screen) still
        // carries the copy, and would put it back when the chat is shown.
        if (pendingComposerPrepends.has(forChatId)) {
          pendingComposerPrepends.set(forChatId, withoutRestored);
        }
        // The delivered attachments go regardless of whether text remains. Keying this off the
        // text emptying meant a Retry after the user had typed something left the files behind,
        // to be sent again with whatever they wrote next.
        restoreComposerAttachments(
          forChatId,
          getComposerAttachments(forChatId).filter((attachment) => !deliveredIds.has(attachment.localId)),
        );
        if (currentChatId === forChatId) {
          messageText = withoutRestored;
        }
      }

      function clearActionFailureNotice(): void {
        actionFailure = null;
        actionFailureRecovery = null;
        actionFailureLaunch = null;
        actionFailureInFlight = null;
        actionFailureAskFailure = null;
        externalRetry = null;
        externalDescribe = null;
        externalAction = null;
        actionFailureActions = [];
        actionFailureKind = "unknown";
        actionFailureServerKind = null;
        isActionFailureOnSignedOutAccount = false;
        actionFailureTitle = "Couldn't send your message";
      }

      /** Tell a refused or unanswered send, with what the buttons need to know about it. */
      function noteSendFailure(error: unknown): void {
        actionFailureTitle = "Couldn't send your message";
        actionFailure = describeSendFailure(error);
        actionFailureAskFailure = null;
        // The reason can change between attempts -- a blocked input can become an agent that is gone -- and the
        // buttons follow the kind, so it is read with the text every time.
        actionFailureKind = composerFailureKindOf(error);
        actionFailureServerKind = describeRequestFailure(error).kind;
        isActionFailureOnSignedOutAccount = actionFailureKind === "account_signed_out";
        actionFailureActions =
          actionFailureKind === "chat_gone" ? [{ label: "Start a new chat", run: startNewChatInsteadOfGoneOne }] : [];
      }

      /** What an agent is asked to fix when ``failingChatId`` has no agent it could start: the chat by the name
       *  the user knows it by, and what the notice holds behind "See details". */
      function failedStartFixRequest(failingChatId: string, details: string | null): AgentFixRequest {
        const name = getChatById(failingChatId)?.title ?? getProvisionalChat(failingChatId)?.name ?? "";
        const chat = name === "" ? "A chat" : `The chat "${name}"`;
        return {
          whatFailed:
            actionFailureKind === "agent_start_failed"
              ? `${chat} couldn't take a message, because its agent couldn't be started`
              : `${chat} couldn't be started`,
          details,
        };
      }

      /** The notice's "Ask an agent to fix this": hand the failure to ``chatToAskId``'s agent, which closes the
       *  notice and shows that chat. A refused ask says why in the notice, under the failure it was about. */
      async function askRunningAgentToFix(chatToAskId: string, request: AgentFixRequest): Promise<void> {
        if (actionFailureInFlight !== null) return;
        const noticeAsked = actionFailure;
        actionFailureInFlight = "ask";
        actionFailureAskFailure = null;
        m.redraw();
        const outcome = await askAgentToFix(askConnectionToChat(chatToAskId), request);
        // The composer moved to another chat meanwhile, which closed the notice.
        if (actionFailure !== noticeAsked) return;
        if (outcome.isOpened) {
          clearActionFailureNotice();
        } else {
          actionFailureInFlight = null;
          actionFailureAskFailure = outcome.failure;
        }
        m.redraw();
      }

      /** A Try again that closes the notice and runs ``run``, which raises its own notice if it fails again. */
      function tryAgainAction(run: () => void): NoticeAction {
        return {
          label: "Try again",
          run: () => {
            clearActionFailureNotice();
            m.redraw();
            run();
          },
        };
      }

      /** A new chat in place of this one, for a send whose chat is gone. The message stays in this composer, for the
       *  user to copy. */
      function startNewChatInsteadOfGoneOne(): void {
        const goneChatId = currentChatId;
        clearActionFailureNotice();
        m.redraw();
        if (goneChatId === null) {
          openProviderChooser();
          return;
        }
        startNewChatInPlaceOf(goneChatId);
      }

      /** Cancel: give the message back and close. Also what Escape and a backdrop press do. */
      function dismissActionFailureNotice(): void {
        // Never dismiss out from under a running action: the send it started is still in flight
        // and will report its own outcome.
        if (actionFailureInFlight !== null) {
          return;
        }
        // The message went back to the composer when the send failed, so there is nothing to
        // restore here -- Cancel just means "leave it there and let me look at it".
        clearActionFailureNotice();
        m.redraw();
        // Hand focus back to where the user was typing, which the send path skipped while the
        // notice was up.
        focusMessageTextarea();
      }

      /** Retry: the ordinary send again, so it re-runs preflight and can fail again. */
      async function retryFailedSend(): Promise<void> {
        if (actionFailureInFlight !== null) {
          return;
        }
        const recovery = actionFailureRecovery;
        const launch = actionFailureLaunch;
        const retryExternal = externalRetry;
        if (recovery === null && launch === null && retryExternal === null) {
          return;
        }
        actionFailureInFlight = "retry";
        m.redraw();
        if (recovery !== null) {
          await repeatFailedSend(recovery);
          return;
        }
        if (launch !== null) {
          await repeatFailedLaunch(launch);
          return;
        }
        // A sibling's operation: it knows how to repeat itself, and is told again the way it was told the
        // first time.
        try {
          await retryExternal!();
          clearActionFailureNotice();
        } catch (err) {
          actionFailure = (externalDescribe ?? describeSendFailure)(err);
          actionFailureKind = describeRequestErrorKind(err);
          actionFailureInFlight = null;
        }
        m.redraw();
      }

      /**
       * Shared tail of Retry and Restart: send the message again and settle the notice.
       *
       * Sends ``sentText`` -- what actually went to the agent, attachment references included --
       * not the typed prose, which would drop the attachments silently.
       */
      async function repeatFailedSend(recovery: SendRecovery): Promise<void> {
        // Paint the same optimistic bubble the normal send path paints, so a retried message is
        // not simply absent from the transcript until the backend catches up.
        const messageId = recovery.messageId;
        const outgoingId = addOutgoing(recovery.chatId, recovery.sentText, messageId);
        let priorTurnIds: ReadonlySet<string> = new Set();
        try {
          refuseSendToFailedChat(recovery.chatId);
          priorTurnIds = sentUserTurnIds(recovery.chatId);
          noteSendToChatNotYetAnAgent(recovery.chatId, messageId, {
            text: recovery.text,
            attachments: recovery.attachments,
            outgoingId,
            isInBox: true,
          });
          const status = await sendMessage(recovery.chatId, recovery.sentText, messageId, true);
          dropOutgoingNoTurnWillRetire(recovery.chatId, outgoingId, status);
          noteOutgoingAccepted(recovery.chatId, outgoingId);
          if (settleSendToChatNotYetAnAgent(recovery.chatId, messageId, status)) {
            // Its chat's discard put it back in the box and said why, so the copy and the notice stay.
            actionFailureInFlight = null;
            m.redraw();
            return;
          }
          // Landed, so take the restored copy back out of the composer.
          clearRestoredMessage(recovery.chatId, recovery.text, recovery.attachments);
          noteSendLeftBox(recovery.chatId, messageId);
          stopWatchingUnconfirmedTurn(recovery);
          clearActionFailureNotice();
          focusMessageTextarea();
        } catch (err) {
          dropOutgoing(recovery.chatId, outgoingId);
          if (settleSendToChatNotYetAnAgent(recovery.chatId, messageId, null)) {
            actionFailureInFlight = null;
            m.redraw();
            return;
          }
          watchForUnconfirmedTurn(err, recovery, priorTurnIds);
          catchUpOnSignedOutAccount(err);
          // Failed again. Only re-open the notice if they are still on that agent -- otherwise
          // it would surface this agent's error over a different chat, with no way to act on it.
          // The message is already back in that agent's composer either way.
          if (currentChatId === recovery.chatId) {
            noteSendFailure(err);
            actionFailureInFlight = null;
          } else {
            clearActionFailureNotice();
          }
        }
        m.redraw();
      }

      /** Restart: restart the agent, then send. Destructive -- it ends any in-progress turn. */
      async function restartFailedSend(): Promise<void> {
        const recovery = actionFailureRecovery;
        if (recovery === null || actionFailureInFlight !== null) {
          return;
        }
        actionFailureInFlight = "restart";
        m.redraw();
        // Rescue anything queued inside the harness first. The restart below SIGKILLs the agent,
        // which drops that queue -- and a feature whose rule is "never lose the message" must not
        // have a button that quietly loses other people's. Best-effort on purpose: the agent we
        // are about to restart is often the one that is stuck, so a drain that fails or is refused
        // must not stop the restart the user actually asked for.
        try {
          const drained = await drainToComposer(recovery.chatId);
          if (drained.block) {
            prependToComposer(recovery.chatId, drained.block);
          }
        } catch {
          // Nothing to do: the restart still goes ahead, and anything queued is lost with it.
        }
        try {
          // The restart itself, unconditionally: the drain does not guarantee one. Claude's
          // empty-queue path is a native chord, and pi/codex hand back a block without ever
          // restarting -- so a non-empty block is not evidence the process was replaced, and a
          // wedged agent is exactly what Restart is for. If this is refused (the services agent
          // carries is_primary=true, say) that refusal becomes the notice's text, nothing is sent.
          await interruptAgent(recovery.chatId);
        } catch (err) {
          actionFailure = plainFailureLines(
            err,
            "This chat's agent could not be restarted, so your message was not resent",
          );
          actionFailureKind = describeRequestErrorKind(err);
          actionFailureInFlight = null;
          m.redraw();
          return;
        }
        await repeatFailedSend(recovery);
      }

      /** Launch a chat awaiting its first send on ``choice``: a room, or an agent on an account. A seeded chat's
       *  message rides the launch as its first; any other is sent once the launch is under way. */
      async function launchForFirstSend(
        launchedChatId: string,
        choice: FirstSendChoice,
        isSeeded: boolean,
        finalText: string,
      ): Promise<void> {
        if (choice.kind === "room") {
          // A room is launched like a chat on an account, on none: the message rides the launch and is
          // appended once the room is up.
          await launchChat(launchedChatId, "", finalText, "room");
        } else {
          await launchChat(launchedChatId, choice.id, isSeeded ? finalText : "", "agent");
        }
      }

      /** Try again for a first send whose launch failed: launch the chat again with the message as its first. */
      async function repeatFailedLaunch(recovery: SendRecovery): Promise<void> {
        const outgoingId = addOutgoing(recovery.chatId, recovery.sentText, recovery.messageId);
        try {
          const awaiting = getProvisionalChat(recovery.chatId);
          if (awaiting === undefined || awaiting.phase !== "awaiting_first_send") {
            // Launched since, possibly by the launch that went unanswered with this message as its first: sending
            // it again could deliver it twice, so it stays in the box for the user to send or not.
            dropOutgoing(recovery.chatId, outgoingId);
            clearActionFailureNotice();
            m.redraw();
            return;
          }
          const choice = await chooseAccountForFirstSend(awaiting);
          if (choice === null) {
            dropOutgoing(recovery.chatId, outgoingId);
            actionFailureInFlight = null;
            m.redraw();
            return;
          }
          await launchForFirstSend(recovery.chatId, choice, true, recovery.sentText);
          clearRestoredMessage(recovery.chatId, recovery.text, recovery.attachments);
          clearActionFailureNotice();
          focusMessageTextarea();
        } catch (err) {
          dropOutgoing(recovery.chatId, outgoingId);
          if (currentChatId === recovery.chatId) {
            actionFailure = describeLaunchFailure(err);
            actionFailureInFlight = null;
          } else {
            clearActionFailureNotice();
          }
        }
        m.redraw();
      }

      function renderActionFailureNotice(
        failure: FailureLines,
        onTurnOnSourceView: (() => void) | undefined,
      ): m.Children {
        const recovery = actionFailureRecovery;
        if (isActionFailureOnSignedOutAccount) {
          // The message is back in the composer; it goes out on the provider chosen here, with the chat's next send.
          const signedOutChatId = recovery?.chatId ?? chatId;
          return m(actionFailureNotice, {
            title: actionFailureTitle,
            body: failure.body,
            details: failure.details,
            dismissLabel: "Cancel",
            onDismiss: dismissActionFailureNotice,
            actions: [
              {
                label: "Choose a provider",
                run: () => {
                  clearActionFailureNotice();
                  m.redraw();
                  if (signedOutChatId) chooseProviderToContinue(signedOutChatId);
                },
              },
            ],
          });
        }
        // The chat app already tried once more for an agent that was down and would not start,
        // and Restart is the stronger way to bring it up, so Retry is not offered; nor for a chat that is gone, whose
        // account was signed out, or that never became an agent, which neither can clear. Every other kind -- and
        // anything unclassified -- keeps it.
        const isResendable = !KINDS_NOTHING_HERE_CAN_CLEAR.has(actionFailureKind);
        const isRepeatable =
          isResendable &&
          actionFailureKind !== "agent_unreachable" &&
          (recovery !== null || actionFailureLaunch !== null || externalRetry !== null);
        // The text was typed and Enter sent but nothing confirmed the agent took it, so it may already be in the
        // agent's input or turn and a resend can deliver it twice. The notice shows its own sentence: mngr's reason
        // for this kind is its diagnostics (the whole pane included).
        const isUnconfirmed = actionFailureKind === "submission_unconfirmed";
        // The chat app never answered the send, so it may have delivered it before the answer was lost. Send again
        // repeats the message's id, which the chat app answers as it answered the first, so it is not sent twice.
        const isNotAnswered = actionFailureKind === "not_answered";
        const retryWording = isUnconfirmed
          ? { label: "Send again", inFlight: "Sending…", tooltip: "Sends the message again, even if the agent has it" }
          : isNotAnswered
            ? { label: "Send again", inFlight: "Sending…", tooltip: "Sends the message again, unless it arrived" }
            : {
                label: recovery === null ? "Try again" : "Retry",
                inFlight: "Retrying…",
                tooltip: "Tries the same thing again",
              };
        // Restart needs a message to send afterwards, so it is offered only for our own send. It survives Retry
        // being withheld for an agent that would not start, whose restart is the stronger way to bring it up.
        const isRestartOffered =
          isResendable && recovery !== null && !KINDS_RESTART_MUST_NOT_ANSWER.has(actionFailureKind);
        const failingChatId = recovery?.chatId ?? chatId;
        // An agent that would not start on this account may start on another; a chat that never became an agent
        // has its own page for that. It waits for the chat list, which can land after the page: the move needs the
        // chat.
        const switchProviderChatId =
          actionFailureKind === "agent_start_failed" &&
          failingChatId !== null &&
          getChatById(failingChatId) !== undefined
            ? failingChatId
            : null;
        const chatToAskId =
          failingChatId === null ? null : chatToAskAboutRefusedSend(actionFailureKind, failingChatId);
        // The agent itself refused, so its terminal shows why.
        const isTerminalUseful =
          onTurnOnSourceView !== undefined &&
          recovery !== null &&
          actionFailureServerKind !== null &&
          KINDS_THE_TERMINAL_SHOWS.has(actionFailureKind);
        // An unconfirmed send's notice is worded here, and its sentence asks for no bug report.
        const shown: FailureLines = isUnconfirmed
          ? {
              body: ["The agent may already have it, so check its terminal before you send it again."],
              details: failure.details,
              isReportable: false,
            }
          : {
              ...failure,
              body: [
                ...failure.body,
                isRestartOffered ? (SEND_FAILURE_RESTART_SENTENCES[actionFailureKind] ?? null) : null,
              ],
            };
        // A refused ask is told as part of the notice, so the notice keeps one "See details" and its bug report
        // files both. The ask itself is handed the failure alone.
        const told = withRefusedAsk(shown, actionFailureAskFailure);
        const notice: NoticeDialogAttrs = {
          title: isUnconfirmed
            ? "Couldn't confirm your message arrived"
            : isNotAnswered
              ? "Your message may not have been sent"
              : actionFailureTitle,
          body: told.body,
          details: told.details,
          dismissLabel:
            isRepeatable ||
            isRestartOffered ||
            actionFailureActions.length > 0 ||
            externalAction !== null ||
            switchProviderChatId !== null ||
            chatToAskId !== null
              ? "Cancel"
              : "OK",
          isDismissable: actionFailureInFlight === null,
          onDismiss: dismissActionFailureNotice,
          actions: [
            ...(isRepeatable
              ? [
                  {
                    label: actionFailureInFlight === "retry" ? retryWording.inFlight : retryWording.label,
                    tooltip: retryWording.tooltip,
                    isDisabled: actionFailureInFlight !== null,
                    run: () => void retryFailedSend(),
                  },
                ]
              : []),
            ...actionFailureActions,
            ...(externalAction === null ? [] : [externalAction]),
            ...(switchProviderChatId !== null
              ? [
                  {
                    label: SWITCH_PROVIDER_LABEL,
                    tooltip: "Moves this chat to another provider. Your message stays in the box.",
                    isSecondary: true,
                    isDisabled: actionFailureInFlight !== null,
                    run: () => {
                      clearActionFailureNotice();
                      m.redraw();
                      chooseProviderToSwitchChat(switchProviderChatId);
                    },
                  },
                ]
              : []),
            ...(failingChatId !== null && chatToAskId !== null
              ? [
                  {
                    label: actionFailureInFlight === "ask" ? ASKING_AN_AGENT_LABEL : ASK_AGENT_TO_FIX_LABEL,
                    tooltip: "Sends what failed to an agent that is already running, and shows its chat",
                    isSecondary: true,
                    isDisabled: actionFailureInFlight !== null,
                    run: () =>
                      void askRunningAgentToFix(chatToAskId, failedStartFixRequest(failingChatId, shown.details)),
                  },
                ]
              : []),
            ...(isTerminalUseful
              ? [
                  {
                    label: "Turn on Source view",
                    tooltip: "Shows the agent's terminal, where you can see what it is waiting on",
                    isSecondary: true,
                    run: () => {
                      dismissActionFailureNotice();
                      onTurnOnSourceView();
                    },
                  },
                ]
              : []),
            ...reportBugActions(getShellConnection(), told),
            ...(!isRestartOffered
              ? []
              : [
                  {
                    label: actionFailureInFlight === "restart" ? "Restarting…" : "Restart",
                    tooltip: "Restarts the agent, then sends your message. Anything it's working on stops.",
                    isDestructive: true,
                    isDisabled: actionFailureInFlight !== null,
                    run: () => void restartFailedSend(),
                  },
                ]),
          ],
        };
        return m(actionFailureNotice, notice);
      }

      /**
       * What the armed switch will do, above the composer (spec 5.1): the account the next message
       * moves the chat to and the model picked for it, with a way back into the dialog to change the
       * pick and a way to call the choice off.
       */
      function renderSwitchStrip(target: ProviderAccount): m.Children {
        const pick = chatId ? getPendingPick(chatId) : null;
        const destination = pick === null ? target.label : `${target.label}, ${pick.label}`;
        return m(
          "div",
          {
            class:
              "message-input-switch-strip mx-1 mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 " +
              "text-(length:--font-size-helper) text-secondary",
            role: "status",
          },
          [
            m(
              "span",
              { class: "message-input-switch-strip-text" },
              `Your next message switches this chat to ${destination}`,
            ),
            m(
              Button,
              {
                variant: "ghost",
                sm: true,
                extra: "message-input-switch-change",
                onclick: () => {
                  if (chatId) openSwitchDialog(chatId, target);
                },
              },
              "Change",
            ),
            m(
              Button,
              {
                variant: "ghost",
                sm: true,
                extra: "message-input-switch-cancel",
                onclick: () => {
                  if (chatId) setPendingAccount(chatId, null);
                },
              },
              "Cancel",
            ),
          ],
        );
      }

      /** What dictation has to say, over the composer: Retry runs held words again, or is the mic pressed again,
       *  which asks for the speech model when its load failed and starts dictation when the microphone did. */
      function renderDictationNotice(problem: DictationProblem): m.Children {
        return dictationNotice(problem, {
          onRetry: () => {
            if (dictation?.isHeld) dictation.retryHeld();
            else handleMicPress();
            m.redraw();
          },
          onDiscard: () => {
            dictation?.discardHeld();
            m.redraw();
          },
          onDismiss: () => {
            dictationProblem = null;
            m.redraw();
          },
        });
      }

      function renderDeclinedCommandNotice(
        declined: { title: string; body: string; isSourceViewOffered: boolean },
        onTurnOnSourceView: (() => void) | undefined,
      ): m.Children {
        return m(declinedCommandNotice, {
          title: declined.title,
          body: [declined.body],
          dismissLabel: "OK",
          onDismiss: dismissDeclinedCommandNotice,
          // The command still works in the agent's own terminal, a switch away.
          actions:
            onTurnOnSourceView === undefined || !declined.isSourceViewOffered
              ? []
              : [
                  {
                    label: "Turn on Source view",
                    tooltip: "Shows the agent's terminal, where the command can be typed",
                    isSecondary: true,
                    run: () => {
                      dismissDeclinedCommandNotice();
                      onTurnOnSourceView();
                    },
                  },
                ],
        });
      }

      function renderAuthCommandNotice(command: string): m.Children {
        return m(authCommandNotice, {
          title: command === "/logout" ? "Sign-out is managed here" : "Sign-in is managed here",
          body: [
            "Sign in from the provider list instead -- signing in through the agent leaves this chat unable to see " +
              "the result.",
          ],
          dismissLabel: "Cancel",
          onDismiss: dismissAuthCommandNotice,
          actions: [
            {
              label: "Open providers",
              run: () => {
                dismissAuthCommandNotice();
                openProviderChooser();
              },
            },
          ],
        });
      }

      /** In place of the composer while the chat's account is signed out: nothing more goes to it. */
      function renderSignedOutAccountNotice(signedOutChatId: string): m.Children {
        return providerNotice(
          "message-input-signed-out",
          "You signed out of the account this chat runs on. Choose a provider to continue it on.",
          () => chooseProviderToContinue(signedOutChatId),
        );
      }

      /**
       * The chooser for a chat with no provider to start on: the sign-in it produces launches a chat still
       * awaiting its first send right away, so its agent is up before the user has typed, and the first send
       * goes to the running agent (carrying the seeded conversation, for a seeded chat). A launch the chat app
       * refuses lands on the page as a failed create, with its "Try again".
       */
      function openProviderChooserForFirstSend(awaitingChatId: string): void {
        openProviderChooser({
          onSignedIn: (accountId) => {
            const awaiting = getProvisionalChat(awaitingChatId);
            if (awaiting?.phase !== "awaiting_first_send" || getChatById(awaitingChatId) !== undefined) return;
            if (isLaunchInFlight(awaitingChatId)) return;
            launchChat(awaitingChatId, accountId, "", "agent").catch((error: unknown) => {
              console.error(
                `The chat ${awaitingChatId} could not be started on sign-in: ${describeRequestError(error)}`,
              );
            });
          },
          onRoomChosen: () => void launchAwaitingChatAsRoom(awaitingChatId),
        });
      }

      /** In place of the composer while nothing is signed in that a new chat could start on. The chooser opens as the
       *  notice appears, and its button opens it again once dismissed. */
      function renderNoProviderNotice(awaitingChatId: string): m.Children {
        return providerNotice(
          "message-input-no-provider",
          "Connect an AI provider to start chatting.",
          () => openProviderChooserForFirstSend(awaitingChatId),
          () => openProviderChooserForFirstSend(awaitingChatId),
        );
      }

      const attachments = getComposerAttachments(chatId);
      const hasMessageText = messageText.trim().length > 0;
      const canSend = hasMessageText || hasReadyAttachments(chatId);

      const chat = getChatById(chatId);
      // The switch the chat is in the middle of, if any, and the one the next send would start.
      const handoff = chat?.handoff ?? null;
      const switchTarget = nextSendSwitchTarget(chatId);
      // A draft typed while the switch request is out waits for it: sent now, it would reach the
      // agent the chat is leaving, ahead of the switch.
      const isSendHeldForSwitch = isSwitchSending(chatId);
      // A chosen provider arms a switch, and the next send carries the chat to it, so the composer
      // comes back as soon as one is armed, and stays while that switch is sent.
      const isBlockedBySignedOutAccount =
        handoff === null &&
        switchTarget === null &&
        !isSendHeldForSwitch &&
        isAccountSignedOut(chat?.active_agent.account_id);
      // Until the account list has loaded the composer shows as usual: most pages have an account.
      const awaitingFirstSend = chat === undefined ? getProvisionalChat(chatId) : undefined;
      const isBlockedByNoProvider =
        awaitingFirstSend?.phase === "awaiting_first_send" &&
        areAccountsLoaded() &&
        accountForFirstSend(awaitingFirstSend.account_id) === null;
      const isComposerDisabled = isBlockedBySignedOutAccount || isBlockedByNoProvider;
      isComposerDisabledNow = isComposerDisabled;
      const canSendNow = canSend && !isComposerDisabled;
      // The box is read-only while dictation owns it, from the press until the last words land.
      const isDictating = dictationState !== "idle";
      // The send that waited for the dictation's words goes once they are in, after this pass has drawn them.
      if (isSendPendingAfterDictation && !isDictating) {
        isSendPendingAfterDictation = false;
        queueMicrotask(() => void handleSubmit());
      }
      const blockingNotice = isBlockedBySignedOutAccount
        ? renderSignedOutAccountNotice(chatId)
        : isBlockedByNoProvider && chatId !== null
          ? renderNoProviderNotice(chatId)
          : null;

      // The stop button is there whenever a message of the user's is in progress: while the
      // agent has an interruptible turn -- the same condition that drives the status row at the
      // end of the transcript, read straight off the backend-derived activity state -- and while a sent
      // message waits for the agent to come up, when Stop takes it back. While the chat switches
      // harness the turn in progress is the switch's own (the summary it asked for), so the button
      // is the switch's cancel instead, for as long as that is possible.
      const activityState = chat?.active_agent.activity_state ?? null;
      const isAgentWorking = isWorkingActivityState(activityState);
      const isConnecting = isChatConnecting(chatId);
      const isUninterruptibleCompaction =
        activityState === COMPACTING_STATE &&
        getHarnessCatalog(chat?.active_agent.harness)?.can_interrupt_compaction === false;
      const isStopButtonVisible =
        (isAgentWorking || isConnecting) && !isInterruptInFlight && handoff === null && !isUninterruptibleCompaction;
      const isCancelSwitchVisible = handoff !== null && isHandoffCancellable(handoff);
      // Read straight off the backend's queue snapshot -- the frontend holds no queued state.
      const hasQueuedMessages = (chat?.active_agent.queued_messages ?? []).length > 0;
      const stopButtonLabel =
        isConnecting && !isAgentWorking
          ? "Stop sending and bring your message back to draft area"
          : hasQueuedMessages
            ? "Interrupt agent and bring queued messages to draft area"
            : "Interrupt agent";

      const isDictationShown = isDictationOffered();
      return m(
        "div",
        {
          class: "message-input mx-auto w-full max-w-(--width-message-column)",
          oncreate: isDictationShown ? attachDictationToPage : undefined,
          onremove: isDictationShown ? detachDictationFromPage : undefined,
        },
        [
          interceptedAuthCommand !== null ? renderAuthCommandNotice(interceptedAuthCommand) : null,
          declinedSend !== null ? renderDeclinedCommandNotice(declinedSend, vnode.attrs.onTurnOnSourceView) : null,
          actionFailure !== null ? renderActionFailureNotice(actionFailure, vnode.attrs.onTurnOnSourceView) : null,
          dictationProblem !== null ? renderDictationNotice(dictationProblem) : null,
          switchTarget !== null ? renderSwitchStrip(switchTarget) : null,
          blockingNotice,
          m("input", {
            type: "file",
            multiple: true,
            class: "message-input-file-input hidden",
            oncreate: (inputVnode: m.VnodeDOM) => {
              fileInputElement = inputVnode.dom as HTMLInputElement;
            },
            onremove: () => {
              fileInputElement = null;
            },
            onchange: (event: Event) => {
              const input = event.target as HTMLInputElement;
              uploadFilesToComposer(chatId, input.files);
              input.value = "";
            },
          }),
          composeRow(
            vnode.attrs.leading,
            // With no provider to send to, the composer stays in view, holding any draft, but takes nothing.
            [
              mentionPopover({
                rows: mentionRows(),
                selectedIndex: mentionDraft?.index ?? 0,
                onPick: pickMention,
                onHover: (index) => {
                  if (mentionDraft !== null) mentionDraft = { ...mentionDraft, index };
                },
              }),
              m(
                "div",
                {
                  class: MESSAGE_ROW_CLASS,
                  "data-attachments": attachments.length > 0 ? "" : undefined,
                  oncreate: attachMessageRow,
                  onremove: detachMessageRow,
                },
                [
                  m("textarea", {
                    class:
                      "message-input-textbox col-start-2 row-start-1 w-full resize-none border-none bg-transparent " +
                      "px-1 py-1 font-sans text-(length:--font-size-body) leading-[1.4] text-primary focus:outline-none " +
                      "placeholder:text-faint group-data-[stacked]/row:col-span-3 group-data-[stacked]/row:col-start-1 " +
                      "disabled:cursor-not-allowed disabled:opacity-60",
                    // A switch off a room holds the message like any switch, so its wording comes first.
                    placeholder:
                      handoff !== null
                        ? handoffComposerPlaceholder(handoff)
                        : composerHint !== null
                          ? composerHint
                          : isRoomChat(chatId)
                            ? "Message (tag @agent to ask the agent)"
                            : isAgentWorking
                              ? "Type to queue more messages..."
                              : "Type a message...",
                    rows: 1,
                    disabled: isComposerDisabled,
                    readonly: isDictating,
                    value: messageText,
                    oncreate: (textareaVnode: m.VnodeDOM) => {
                      messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                      mountedComposerTextarea = messageTextareaElement;
                      wasComposerDisabled = isComposerDisabled;
                      refitComposer();
                      focusMessageTextarea();
                    },
                    onupdate: (textareaVnode: m.VnodeDOM) => {
                      messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                      mountedComposerTextarea = messageTextareaElement;
                      refitComposer(isTypingUntilRedraw);
                      isTypingUntilRedraw = false;
                      if (wasComposerDisabled && !isComposerDisabled) focusMessageTextarea();
                      wasComposerDisabled = isComposerDisabled;
                    },
                    onremove: () => {
                      if (mountedComposerTextarea === messageTextareaElement) mountedComposerTextarea = null;
                      messageTextareaElement = null;
                    },
                    oninput: (event: Event) => {
                      const textarea = event.target as HTMLTextAreaElement;
                      messageText = textarea.value;
                      localStorage.setItem(messageTextKey(chatId), messageText);
                      updateMentionDraft(textarea);
                      refitAfterEdit();
                      if (messageText.trim() !== "") warmUpStoppedChat(chatId);
                      // Text entered while the speech model's load is shown is the user going back to typing. A
                      // press or a tap in the box alone is not: it leaves the load shown, and sets where the
                      // dictated words will land.
                      dismissModelLoad();
                    },
                    onclick: (event: MouseEvent) => updateMentionDraft(event.target as HTMLTextAreaElement),
                    onblur: () => {
                      mentionDraft = null;
                    },
                    onkeydown: handleKeydown,
                    onpaste: handlePaste,
                  }),
                  m(
                    Button,
                    {
                      variant: "ghost",
                      icon: true,
                      sm: true,
                      extra: ATTACH_BUTTON_CLASS,
                      ...hoverTooltipAttrs("Attach files", "above"),
                      "aria-label": "Attach files",
                      disabled: isComposerDisabled,
                      onclick: openFilePicker,
                    },
                    m.trust(icon("attach", { size: 16, strokeWidth: 1.5 })),
                  ),
                  attachments.length > 0
                    ? m(
                        "div",
                        { class: "col-span-3 col-start-1 row-start-2 min-w-0 pt-1" },
                        m(AttachmentStrip, {
                          children: attachments.map((attachment) => renderComposerAttachment(chatId, attachment)),
                        }),
                      )
                    : null,
                  m("div", { class: TOOLBAR_CLASS }, [
                    isCancelSwitchVisible
                      ? m(
                          Button,
                          {
                            variant: "secondary",
                            sm: true,
                            extra: "message-input-cancel-switch-button shrink-0",
                            readonly: isCancelSwitchInFlight,
                            ...hoverTooltipAttrs(
                              "Keep this chat on its current agent; your message comes back here",
                              "above",
                            ),
                            "aria-label": "Cancel switch",
                            onclick: () => void handleCancelSwitch(),
                          },
                          isCancelSwitchInFlight ? "Cancelling…" : "Cancel switch",
                        )
                      : null,
                    isStopButtonVisible
                      ? m(
                          Button,
                          {
                            variant: "stop",
                            icon: true,
                            sm: true,
                            extra: "message-input-stop-button shrink-0",
                            // The label states what THIS press will do. The button always interrupts;
                            // it only hands messages back when there are some parked in the harness,
                            // so promising that unconditionally described a case that usually is not
                            // the one in front of the user.
                            ...hoverTooltipAttrs(stopButtonLabel, "above"),
                            "aria-label": stopButtonLabel,
                            onclick: handleStopToComposer,
                          },
                          m.trust(stopIcon(14)),
                        )
                      : null,
                    isDictationShown
                      ? m(DictationButton, {
                          state: micState(),
                          progress: speechModelProgress?.getCurrentResult().data ?? null,
                          download:
                            isSpeechModelDownloaded === false ? { totalBytes: speechModelDownloadBytes } : null,
                          shortcutLabel: dictationShortcutLabel(isAppleKeyboard()),
                          disabled: isComposerDisabled,
                          onPress: handleMicPress,
                          onDismissLoading: handleLoadingPress,
                          onMeter: (setLevel) => {
                            setMeterLevel = setLevel;
                          },
                        })
                      : null,
                    // The send button reads "Switch and send" while a pending lane differs from the
                    // chat's harness (spec 5.1): the next send moves the chat, so the button says so.
                    // Otherwise it stays in place: the green send button, or with nothing it can send a
                    // disabled ghost -- the bare arrow, faded.
                    canSendNow && switchTarget !== null
                      ? m(
                          Button,
                          {
                            variant: "primary",
                            sm: true,
                            extra: "message-input-send-button message-input-send-button--switch shrink-0",
                            ...hoverTooltipAttrs(`Switch this chat to ${switchTarget.label} and send`, "above"),
                            "aria-label": "Switch and send",
                            onclick: handleSubmit,
                          },
                          [
                            m("span", "Switch and send"),
                            m(
                              "span",
                              { class: "ml-1.5 inline-flex items-center" },
                              m.trust(icon("send", { size: 14, strokeWidth: 2.5 })),
                            ),
                          ],
                        )
                      : m(
                          Button,
                          {
                            variant: canSendNow ? "primary" : "ghost",
                            icon: true,
                            sm: true,
                            extra: "message-input-send-button shrink-0",
                            disabled: !canSendNow && !isSendPendingAfterDictation,
                            // While the dictation's last words are landing, the send waits and says so (spec D8).
                            readonly: isSendHeldForSwitch || isSendPendingAfterDictation,
                            ...hoverTooltipAttrs(
                              isSendPendingAfterDictation
                                ? "Finishing your words"
                                : !canSendNow
                                  ? null
                                  : isSendHeldForSwitch
                                    ? "Sending is paused until the switch starts"
                                    : "Send message",
                              "above",
                            ),
                            "aria-label": "Send message",
                            onclick: handleSubmit,
                          },
                          isSendPendingAfterDictation
                            ? m.trust(spinnerIcon(16))
                            : m.trust(icon("send", { size: 16, strokeWidth: 2 })),
                        ),
                  ]),
                ],
              ),
            ],
          ),
        ],
      );
    },
  };
}
