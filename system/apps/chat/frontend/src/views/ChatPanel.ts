/**
 * Chat panel: the main message list and message input for an agent, the whole of the chat
 * page's front face.
 *
 * A chat that is not an agent yet (a provisional chat) renders by its phase: waiting for an
 * account, it offers the provider chooser; being created, it shows the composer over an
 * empty transcript (a message typed now is held until the agent lands); failed, it shows the
 * reason and a way to try again over the composer, so a message held through the failure is
 * back in it where it can be seen. The transcript takes over when creation completes. A
 * seeded chat (one opened on turns written before any agent joined it) has a transcript from
 * the start, so it renders as one through its wait for the first send and its create; only a
 * failed create shows the provisional screen.
 *
 * In the phone layout (``isCompact``) the model menu opens from a settings button at the
 * composer's left rather than the chip in the under-bar, and carries the Source view switch as
 * one of its rows; the under-bar keeps the fast-mode notice, and the switch while the terminal
 * face is up, since the composer and its button turn over with the chat face. What the agent is
 * doing -- a send on its way or connecting, a turn running, closing or done -- is one status row
 * at the end of the transcript, where the reply appears.
 */

import m from "mithril";
import { isSlotClaimed } from "../slots";
import {
  addMessageSentListener,
  evictEvents,
  fetchBackfillEvents,
  fetchEvents,
  fetchForwardEvents,
  fetchWindowAtOffset,
  getConversationLoadState,
  getEventsForChat,
  getEventCount,
  getFirstOffset,
  getLastEventId,
  getRenderVersion,
  getTotalEventCount,
  hasMoreAfter,
  isConversationNotFound,
  noteLoadedArrivals,
  removeMessageSentListener,
  type ToolResultEvent,
  isTranscriptLoaded,
} from "../models/Response";
import type { FillAction } from "../models/transcriptScroll/fillPlanner";
import { createTranscriptScrollEngine } from "./transcript-scroll-engine";
import { TranscriptScrollbar } from "./TranscriptScrollbar";
import { connectToStream, disconnectFromStream, loadSnapshotWithStream } from "../models/StreamingMessage";
import { shownReplyDraft } from "../models/ReplyDrafts";
import { renderAssistantMessage } from "./message-renderers";
import {
  addChatsUpdatedListener,
  getChatById,
  getProvisionalChat,
  hasReceivedChatList,
  isRoomChat,
  launchChat,
  removeChatsUpdatedListener,
} from "../models/Chats";
import type { ProvisionalChat } from "../models/Chats";
import { accountForFirstSend, openProviderChooser, whenAccountsReadyToChoose } from "../models/Providers";
import { maybeApplyFastModeLimit } from "./fast-mode-limit";
import { FastModeNotice } from "./FastModeNotice";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { EmptySlot } from "./EmptySlot";
import { uploadFilesToComposer } from "../models/ComposerAttachments";
import { MessageInput, startNewChatInPlaceOf } from "./MessageInput";
import { isAgentProcessDead } from "./agentLiveness";
import { getShellConnection } from "../shellConnection";
import { destroyChat, startChat } from "../root/verbs";
import { ModelProviderMenu } from "./ModelProviderMenu";
import { AgentTerminalPanel } from "./AgentTerminalPanel";
import { chatFlipCard } from "./chat-flip";
import { TerminalViewToggle } from "./TerminalViewToggle";
import { buildAgentTerminalUrl } from "../models/Chats";
import { getShellHandshake, reportChatOnScreenOnce } from "../shell";
import {
  buildConversationView,
  MESSAGE_LIST_CLASS,
  renderAgentTurnRow,
  renderTranscriptSegments,
  type RowDescriptor,
} from "./conversation-rows";
import { ActivityIndicator, runningToolCall, turnStatusPlacement } from "./ActivityIndicator";
import { authorOfTurn } from "./user-message-display";
import { TurnDoneMark } from "./TurnDoneMark";
import type { AssistantMessageEvent, TranscriptEvent } from "../models/Response";
import { getResentSendsKey, resentTurnsIn } from "../models/ResentSends";
import { requestFrameFocus } from "@imbue/workspace-ui/src/terminalFocus";
import { renderQueuedMessages } from "./QueuedMessageView";
import { closedPendingRun, renderOutgoingMessages, type PendingBubbleRun } from "./OutgoingMessageView";
import { renderHeldSends, renderProvisionalHeldSends } from "./HeldSendView";
import { HandoffFailedNotice } from "./HandoffFailedNotice";
import { AgentWorkingReport } from "./AgentWorkingReport";
import { answeredReplySuggestions } from "./AnsweredReplySuggestions";
import { ChatSuggestions } from "./ChatSuggestions";
import { SuggestionLayer } from "./SuggestionLayer";
import { renderHandoffTailNode } from "./handoff-node";
import { SwitchDialog } from "./SwitchDialog";
import { hasLandedLiveSwitch, hasOpenHandoffRequest, isTailTurnSettled, liveCompactionOf } from "./turn-grouping";
import { closeRunningCompactionChips } from "./compaction-chips";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import {
  AskAgentToFixButton,
  FailureRemedies,
  ReportBugButton,
} from "@imbue/workspace-ui/src/components/FailureButtons";
import type { ReportedFailure } from "@imbue/workspace-ui/src/app_contract";
import { askConnectionToChat, chatToAskAboutFailedCreate } from "./ask-running-agent";
import { SWITCH_PROVIDER_LABEL, chooseProviderInPlaceOf } from "./provider-switch";
import {
  joinDetailLines,
  plainFailureLines,
  plainFailureLinesOf,
  plainFailureSentence,
  shownFailureOfLines,
} from "./failure-lines";
import type { FailureLines } from "./failure-lines";

// The one quiet line an empty conversation shows while its chat starts, and once it is up.
const STARTING_STATUS = "Starting your chat…";
const READY_STATUS = "Your chat is ready.";
const EMPTY_CONVERSATION_STATUS_CLASS =
  "message-list-status pointer-events-none absolute inset-x-0 top-1/3 text-center text-sm text-secondary";
// The failed-create page's buttons. The shared ask button draws a refused ask under itself as a block of its own
// (`ask-agent-failure`), which is centred from here like the rest of the page.
const FAILED_CREATE_BUTTONS_CLASS =
  "flex flex-wrap items-center justify-center gap-2 " +
  "[&_.ask-agent-failure]:flex [&_.ask-agent-failure]:flex-col [&_.ask-agent-failure]:items-center " +
  "[&_.ask-agent-failure-message]:text-center";

/** What the failed-create page says under its heading: a sign-in for a chat that names no account, else what failed,
 *  worded by the failure's kind, and the page's ways out. */
function failedCreateSentence(provisional: ProvisionalChat): string {
  if (provisional.account_id === "") return "Sign in to a provider to start this chat.";
  return provisional.failure_kind === "codex_plugin_install_refused"
    ? "This chat's agent couldn't be set up. Try again, or switch to another provider."
    : "The agent for this chat couldn't be started. Try again, or switch to another provider.";
}

function getChatTerminalUrl(chatId: string): string {
  // The ttyd dispatch script is invoked as `bash -c "$SCRIPT" <args...>` where
  // the first trailing arg becomes $0 (not $1). ``buildAgentTerminalUrl``
  // emits ``arg=_&arg=agent&arg=<name>`` so the dispatch lands ``agent`` in
  // ``$1`` and the name in ``$2``, mirroring the workdir deep-link pattern.
  // When the agent isn't in the local cache yet, the name is empty and
  // agent.sh attaches to the ambient session. A window viewed on somebody
  // else's desktop attaches as a viewer, as the terminal app's own wrapper does.
  const agentName = getChatById(chatId)?.active_agent.name;
  return buildAgentTerminalUrl(agentName || null, getShellHandshake()?.isViewing === true);
}

/** The provisional record of a chat that is not an agent yet, or null once the app lists it
 *  as one. The provisional list is rebuilt from pushes and can still name an agent that has since
 *  registered (a `provisional_chat_created` for a finished creation, delivered late), so the
 *  chat list wins: every branch asks this, so none can show a registered chat as provisional. */
function provisionalRecord(chatId: string): ProvisionalChat | null {
  const provisional = getProvisionalChat(chatId);
  return provisional !== undefined && getChatById(chatId) === undefined ? provisional : null;
}

/** Whether a provisional chat's page is its transcript rather than a provisional screen: a seeded
 *  chat has one to show from the start, and keeps showing it through its create; a failed create
 *  shows its reason instead, like any other. */
export function isSeededTranscriptShown(provisional: ProvisionalChat | null): boolean {
  return provisional !== null && provisional.is_seeded && provisional.phase !== "failed";
}

const NO_TOOL_RESULTS: Map<string, ToolResultEvent> = new Map();

/** The chat's reply draft, while the window reaches the live end, or null when there is none to show. */
function liveReplyDraft(chatId: string): AssistantMessageEvent | null {
  return hasMoreAfter(chatId) ? null : shownReplyDraft(chatId, getLastEventId(chatId));
}

/** The agent's output past the transcript: a switch the transcript does not show yet (draining, or restarting in
 *  place; once the summary request or the landed switch is on the stream the rows carry it), then the reply the agent is still writing,
 *  while the window reaches the live end and no step is live to show it in. Not a row of its own: a draft changes a
 *  few times a second, and a row would re-walk the whole held transcript on every change. `opensStretch`: the nodes
 *  start a stretch of their own rather than carry on the last row's. */
function renderAgentLiveTail(chatId: string, opensStretch: boolean, isReplyDraftInStep: boolean): m.Vnode[] {
  const events = getEventsForChat(chatId);
  const handoff = getChatById(chatId)?.handoff ?? null;
  const handoffNode = renderHandoffTailNode(
    chatId,
    hasOpenHandoffRequest(events, handoff) || hasLandedLiveSwitch(events, handoff),
  );
  const draft = isReplyDraftInStep ? null : liveReplyDraft(chatId);
  return [
    ...(handoffNode === null ? [] : [handoffNode]),
    ...(draft === null
      ? []
      : [renderAssistantMessage(draft, NO_TOOL_RESULTS, chatId, opensStretch && handoffNode === null)]),
  ];
}

/** Whether the last row holds the live end of the transcript: the row is an open stretch of the agent's output, and
 *  the held window reaches the live end, so no unloaded events lie between the two. */
function isLiveTailInLastRow(chatId: string, rows: readonly RowDescriptor[]): boolean {
  return !hasMoreAfter(chatId) && (rows[rows.length - 1]?.takesLiveTail ?? false);
}

/** Whether the call the agent is running shows as a running chip on the last row's line, which then says it in place
 *  of the status row. */
function isRunningCallInline(chatId: string, rows: readonly RowDescriptor[]): boolean {
  const running = runningToolCall(chatId);
  return (
    running !== null &&
    isLiveTailInLastRow(chatId, rows) &&
    (rows[rows.length - 1]?.chipCallIds.has(running.tool_call_id) ?? false)
  );
}

/** The run the pending sends carry on: that of the person whose turn is the last thing said above them (the last
 *  row is their turn, the window reaches the live end, and no stretch of the agent's output is drawn after it), else
 *  none. */
function pendingRunAfterRows(chatId: string, rows: readonly RowDescriptor[], liveEnd: LiveEnd): PendingBubbleRun {
  const turn = rows[rows.length - 1]?.speakerTurn ?? null;
  if (turn === null || hasMoreAfter(chatId) || liveEnd.isAgentTurn) return closedPendingRun();
  return { isOpen: true, author: authorOfTurn(turn) };
}

/** The live end drawn after the rows, and whether it is a stretch of the agent's output of its own. */
interface LiveEnd {
  readonly nodes: m.Children[];
  readonly isAgentTurn: boolean;
}

/** The agent's nodes as a stretch of their own, or nothing when there are none. */
function asAgentTurn(nodes: m.Children[]): m.Vnode[] {
  return nodes.length === 0 ? [] : [renderAgentTurnRow("live-agent-turn", nodes)];
}

export function ChatPanel(): m.Component<{ chatId: string; isVisible?: boolean; isCompact?: boolean }> {
  let currentChatId: string | null = null;
  let panelElement: HTMLElement | null = null;

  // Whether the page's frame is laid out. The chat root keeps the frames of chats it is not
  // showing mounted with `display: none` and mithril redraws globally, so the component keeps
  // running while hidden against an element collapsed to zero size; running scroll work then
  // would corrupt the retained scroll position. The page feeds the frame's layout in via the
  // ``isVisible`` attr (see isFrameRendered in shell.ts); the scroll hooks below skip
  // while it is false.
  // Defaults to true so the panel works before the first render sets it.
  let panelVisible = true;
  // Memoized turn-grouping output. buildSections walks the whole held
  // transcript, so it is recomputed only when the data actually changes (keyed
  // on the render version + idle flag), not on every scroll-driven redraw.
  let rowsCacheKey: string | null = null;
  let cachedRows: RowDescriptor[] = [];
  // The layer of open suggestion pills over the transcript's end, which the scroll engine reads.
  let suggestionLayerElement: HTMLElement | null = null;
  // Whether the cached rows show the reply draft, in the step live at their tail.
  let isReplyDraftInStep = false;

  // The scroll engine owns everything about scrolling: the FOLLOW /
  // USER_CONTROLLED state machine, anchor positioning, spacer sizing, the
  // custom scrollbar mapping, progressive fill (paging, jumps, eviction),
  // persistence, and the ?debug=scroll trace. This panel only feeds it data
  // (via the data source below) and renders the rows/spacers it asks for.
  const engine = createTranscriptScrollEngine({
    isVisible: () => panelVisible,
    getEndOverlayEl: () => suggestionLayerElement,
    dataSource: {
      getRows: () => cachedRows,
      getWindowEventIds: () => getEventsForChat(currentChatId ?? "").map((event) => event.event_id),
      getFirstOffset: () => getFirstOffset(currentChatId ?? ""),
      // Null until the first window has been placed (renderVersion bumps on
      // placement, including for an empty transcript), so the engine's fill
      // planner never races the initial snapshot+stream load.
      getTotalEvents: () => {
        const chatId = currentChatId ?? "";
        return getRenderVersion(chatId) > 0 ? getTotalEventCount(chatId) : null;
      },
      getRenderVersion: () => getRenderVersion(currentChatId ?? ""),
      executeFill: (action: FillAction): Promise<void> => {
        const chatId = currentChatId;
        if (chatId === null) {
          return Promise.resolve();
        }
        switch (action.kind) {
          case "fetch-tail":
            return fetchEvents(chatId).then(() => {});
          case "fetch-before":
            return fetchBackfillEvents(chatId, action.limit);
          case "fetch-after":
            return fetchForwardEvents(chatId, action.limit);
          case "fetch-at-offset":
            return fetchWindowAtOffset(chatId, action.offset, action.limit);
          case "evict":
            evictEvents(chatId, action.side, action.count);
            return Promise.resolve();
          case "idle":
            return Promise.resolve();
          default:
            return action satisfies never;
        }
      },
    },
  });

  // File drag-and-drop: dropping a file anywhere over the chat stages it as a
  // composer attachment. ``dragDepth`` counts dragenter minus dragleave across
  // nested children so the overlay does not flicker as the cursor moves between
  // transcript rows; the overlay is shown while the depth is positive.
  let dragDepth = 0;
  let isFileDragActive = false;
  // Which face of the card is showing, and whether the back one has ever been built. Per-panel
  // rather than global: two chats open side by side turn over independently.
  let isFlipped = false;
  let hasEverFlipped = false;

  function turnOnSourceView(): void {
    if (!isFlipped) toggleSourceView();
  }

  function toggleSourceView(): void {
    isFlipped = !isFlipped;
    // Turning the card over is the user navigating TO the terminal, so the host grants it
    // focus -- the embedded ttyd client never takes focus on its own (see terminalFocus.ts).
    // Redraw first so a first flip has mounted the back face before the ask.
    if (isFlipped) {
      m.redraw.sync();
      requestFrameFocus(panelElement?.querySelector(".chat-flip-back") ?? null);
    }
  }

  function isFileDrag(event: DragEvent): boolean {
    const types = event.dataTransfer?.types;
    return types !== undefined && Array.from(types).includes("Files");
  }

  function handleDragEnter(event: DragEvent): void {
    if (!isFileDrag(event)) {
      return;
    }
    event.preventDefault();
    dragDepth = dragDepth + 1;
    if (!isFileDragActive) {
      isFileDragActive = true;
      m.redraw();
    }
  }

  function handleDragOver(event: DragEvent): void {
    if (!isFileDrag(event)) {
      return;
    }
    // Required so the element is a valid drop target (the browser otherwise
    // rejects the drop).
    event.preventDefault();
  }

  function handleDragLeave(event: DragEvent): void {
    if (!isFileDrag(event) || dragDepth === 0) {
      return;
    }
    dragDepth = dragDepth - 1;
    if (dragDepth === 0 && isFileDragActive) {
      isFileDragActive = false;
      m.redraw();
    }
  }

  function handleDrop(event: DragEvent, chatId: string): void {
    dragDepth = 0;
    const wasActive = isFileDragActive;
    isFileDragActive = false;
    if (!isFileDrag(event)) {
      if (wasActive) {
        m.redraw();
      }
      return;
    }
    event.preventDefault();
    uploadFilesToComposer(chatId, event.dataTransfer?.files);
    m.redraw();
  }

  // Screen capture state (shown when agent has no conversation)
  let screenContent: string | null = null;
  let screenError: string | null = null;
  let screenLoading = false;
  // The agent a capture has already been attempted for. Set before the request
  // and never cleared for that agent, so an attempt that comes back empty (a
  // crashed agent with no pane to capture, or a 404 while the agent is still
  // being registered) does not re-arm the fetch. The not-found view calls this
  // from every render and the fetch ends in `m.redraw()`, so a guard keyed on
  // the *result* -- as an unset `screenContent` was -- feeds itself: each empty
  // result triggers the redraw that issues the next request, which is an
  // unbounded request loop rather than the one-shot capture the view wants.
  let screenAttemptedChatId: string | null = null;

  // A launch of this provisional chat (the page's Try again) in flight, and how the last one
  // was refused.
  let launchInFlight = false;
  let launchError: FailureLines | null = null;
  // A Remove this chat (a failed create) or a Start this chat (a stopped agent with nothing to show) in flight, and
  // how the last one failed.
  let pageActionInFlight = false;
  let pageActionError: FailureLines | null = null;
  // A failed create's "Ask an agent to fix this" waiting for its answer: the page's other ways out wait with it.
  let askInFlight = false;

  async function fetchScreenCapture(chatId: string): Promise<void> {
    if (screenAttemptedChatId === chatId) {
      return;
    }
    screenAttemptedChatId = chatId;
    screenLoading = true;
    screenContent = null;
    screenError = null;
    try {
      const result = await m.request<{ screen: string | null; error?: string }>({
        method: "GET",
        url: apiUrl("/api/chats/:chatId/screen"),
        params: { chatId, scrollback: "true" },
      });
      screenContent = result.screen;
      screenError = result.error ?? null;
    } catch {
      screenError = "Failed to capture screen";
    } finally {
      screenLoading = false;
      m.redraw();
    }
  }

  /** "Try again" on a failed create: on the account the chat was minted for, else (that account was signed out
   *  since) the account a new chat would start on, else the one the provider chooser produces. */
  async function relaunch(chatId: string, mintedAccountId: string): Promise<void> {
    if (launchInFlight) return;
    launchInFlight = true;
    launchError = null;
    m.redraw();
    await whenAccountsReadyToChoose();
    const account = accountForFirstSend(mintedAccountId);
    if (account === null) {
      launchInFlight = false;
      m.redraw();
      openProviderChooser({ onSignedIn: (accountId) => void launchOn(chatId, accountId) });
      return;
    }
    await launchOn(chatId, account.id);
  }

  async function launchOn(chatId: string, accountId: string): Promise<void> {
    launchInFlight = true;
    try {
      await launchChat(chatId, accountId);
    } catch (error) {
      launchError = plainFailureLines(error, "This chat couldn't be started", {
        notAnswered: "The workspace didn't answer, so Imbue Studio can't tell whether this chat started.",
      });
    } finally {
      launchInFlight = false;
      m.redraw();
    }
  }

  /** Run a page-level action (``run``), telling its failure with ``whatFailed``. */
  function runPageAction(run: () => Promise<void>, whatFailed: string): void {
    if (pageActionInFlight) return;
    pageActionInFlight = true;
    pageActionError = null;
    run()
      .catch((error: unknown) => {
        pageActionError = plainFailureLines(error, whatFailed);
      })
      .finally(() => {
        pageActionInFlight = false;
        m.redraw();
      });
  }

  /** What the failed-create page's "Ask an agent to fix this" asks ``targetChatId`` through
   *  (``askConnectionToChat``), with the page told while the ask is pending. */
  function pageAskConnection(targetChatId: string): ReturnType<typeof askConnectionToChat> {
    const connection = askConnectionToChat(targetChatId);
    return {
      ...connection,
      askAgent: async (text, options, delivery) => {
        askInFlight = true;
        try {
          return await connection.askAgent(text, options, delivery);
        } finally {
          askInFlight = false;
          m.redraw();
        }
      },
    };
  }

  /** A failure line under a page's controls, with its details collapsed beneath it. ``isHelpLeftToPage`` where the
   *  page's own remedies already cover the failure. */
  function renderFailureLines(lines: FailureLines | null, extraClass: string, isHelpLeftToPage = false): m.Children {
    if (lines === null) return null;
    return m("div", { class: `${extraClass} flex flex-col items-center gap-1 text-center` }, [
      m("p", { class: "text-danger text-sm" }, plainFailureSentence(lines)),
      m("div", { class: "text-secondary" }, m(SeeDetails, { details: lines.details })),
      lines.isHelpOwed && !isHelpLeftToPage
        ? m(
            "div",
            { class: "flex flex-wrap items-center justify-center gap-2" },
            m(FailureRemedies, { connection: getShellConnection(), failure: shownFailureOfLines(lines), sm: true }),
          )
        : null,
    ]);
  }

  /** A conversation with nothing in it yet -- being created, loading, or empty -- drawn as the
   *  transcript's own empty list, so a new chat is ready to type into at once. ``stateClass`` names
   *  the state for tests and styles; ``nodes`` are messages already on their way (a send typed while
   *  the chat is created), laid out where the transcript will put them. ``status`` is one quiet line
   *  over an empty list saying where the chat stands; it gives way to the first message. */
  function renderEmptyConversation(stateClass: string, nodes: m.Children[], status: string | null = null): m.Vnode {
    const isStatusShown = status !== null && nodes.length === 0;
    return m("div", { class: `message-list-wrapper ${stateClass}` }, [
      isStatusShown ? m("p", { class: EMPTY_CONVERSATION_STATUS_CLASS }, status) : null,
      m("div", { class: MESSAGE_LIST_CLASS }, nodes),
    ]);
  }

  /** A conversation whose transcript is on its way: the shape of a few turns, faded in only once the
   *  load has taken long enough to notice, so a quick switch shows nothing at all. */
  function renderLoadingConversation(): m.Vnode {
    return renderEmptyConversation("message-list-loading", [
      m("div", { class: "message-list-skeleton", "aria-hidden": "true" }, [
        m("div", { class: "message-list-skeleton-bar message-list-skeleton-bar--user" }),
        m("div", { class: "message-list-skeleton-bar" }),
        m("div", { class: "message-list-skeleton-bar message-list-skeleton-bar--short" }),
      ]),
    ]);
  }

  /** The page of a chat whose transcript is still loading: the starting line for the chat this page just
   *  watched being created (its first load follows the create), the loading shape otherwise. */
  function renderWhileLoading(chatId: string): m.Vnode {
    if (chatId === lastStartingChatId) {
      return renderEmptyConversation(
        "message-list-loading",
        withTurnStatus(chatId, renderOutgoingMessages(chatId)),
        STARTING_STATUS,
      );
    }
    return renderLoadingConversation();
  }

  /** The status row at the live end of the transcript (see ActivityIndicator), keyed like the rows beside it. */
  /** Whether the backend reports the chat doing something the status row shows: an activity, a switch,
   *  or a background task it waits on. */
  function isAgentBusy(chatId: string): boolean {
    const chat = getChatById(chatId);
    if (chat === undefined) return false;
    const agent = chat.active_agent;
    return (
      chat.handoff !== null ||
      (agent.activity_state !== null && agent.activity_state !== "IDLE") ||
      agent.background_tasks.length > 0
    );
  }

  function renderTurnStatus(chatId: string, events: TranscriptEvent[]): m.Children {
    return m(ActivityIndicator, { key: "turn-status", chatId, events });
  }

  /** The status row as list items, so a room contributes no hole to the keyed list: a room has no agent of its
   *  own taking turns (the multiplayer spec, section 7.3). */
  function turnStatusNodes(chatId: string, events: TranscriptEvent[]): m.Children[] {
    return isRoomChat(chatId) ? [] : [renderTurnStatus(chatId, events)];
  }

  /** The status row as the last piece of a stretch of the agent's output, on its line: nothing in a room, or while
   *  the row stands under the user's own sends instead. */
  function stretchStatusNodes(chatId: string, events: TranscriptEvent[], isCallInline: boolean): m.Children[] {
    if (isRoomChat(chatId) || turnStatusPlacement(chatId) === "after-sends") return [];
    return [
      m(ActivityIndicator, {
        key: "turn-status",
        chatId,
        events,
        isInStretch: true,
        isRunningCallInline: isCallInline,
      }),
    ];
  }

  /** The live end of the transcript when the last row does not hold it (the transcript ends on a person's turn or on
   *  a switch to another agent, or the window stops short of the live end): the agent's output past the transcript as
   *  a stretch of its own, the status row on its line, or the status row alone under the last row. */
  function renderLiveEndAfterRows(chatId: string, events: TranscriptEvent[], rows: readonly RowDescriptor[]): LiveEnd {
    if (isLiveTailInLastRow(chatId, rows)) return { nodes: [], isAgentTurn: false };
    const agentNodes = renderAgentLiveTail(chatId, true, isReplyDraftInStep);
    if (agentNodes.length > 0) {
      return { nodes: asAgentTurn([...agentNodes, ...stretchStatusNodes(chatId, events, false)]), isAgentTurn: true };
    }
    return {
      nodes: turnStatusPlacement(chatId) === "after-sends" ? [] : turnStatusNodes(chatId, events),
      isAgentTurn: false,
    };
  }

  /** The sends not yet on the transcript, in the order they show: what the agent has queued, what the chat app
   *  holds through a switch, then this page's own. Each one's first bubble names its sender ("You" for the viewer's
   *  own) unless it carries on the run of the turn drawn above. */
  function renderPendingSends(chatId: string, run: PendingBubbleRun): m.Vnode[] {
    // A room has no agent's terminal to check a send in, and a chat still being created has none yet.
    const hasTerminal = getChatById(chatId) !== undefined && !isRoomChat(chatId);
    return [
      ...renderQueuedMessages(chatId, run),
      ...renderHeldSends(chatId, run),
      ...renderOutgoingMessages(chatId, run, hasTerminal ? turnOnSourceView : undefined),
    ];
  }

  /** Messages already on their way to a chat with no transcript yet, with the row saying where they are. */
  function withTurnStatus(chatId: string, nodes: m.Children[]): m.Children[] {
    return nodes.length === 0 ? nodes : [...nodes, ...turnStatusNodes(chatId, [])];
  }

  // The chat this page last showed as being created: the transcript load that follows its create keeps the
  // starting line up rather than flashing the loading shape between "starting" and "ready".
  let lastStartingChatId: string | null = null;

  /** The page of a chat whose create is running. A message typed now is held by the chat app until the
   *  agent lands (see MessageInput), and shows from its record on any page. */
  function renderStarting(chatId: string): m.Vnode {
    lastStartingChatId = chatId;
    const run = closedPendingRun();
    return renderEmptyConversation(
      "message-list-creating",
      withTurnStatus(chatId, [...renderProvisionalHeldSends(chatId, run), ...renderOutgoingMessages(chatId, run)]),
      STARTING_STATUS,
    );
  }

  /** The messages a failed create kept, above its reason: Try again sends them once the chat starts. */
  function renderFailedCreateHeldSends(chatId: string): m.Vnode[] {
    const held = renderProvisionalHeldSends(chatId);
    return held.length === 0
      ? []
      : [m("div", { class: `${MESSAGE_LIST_CLASS} message-list-held-through-failure` }, held)];
  }

  /** The page of a chat that is not an agent yet, by its phase. */
  function renderProvisional(chatId: string, provisional: ProvisionalChat): m.Vnode {
    if (provisional.phase === "creating") {
      // The create is running, whoever started it: a refusal this page recorded from an earlier
      // attempt (another page's Try again won the race) is over, and must not be shown under a
      // later failure's own reason.
      launchError = null;
      return renderStarting(chatId);
    }
    if (provisional.phase === "awaiting_first_send") {
      // A chat with no seed that waits for its first send (an intake that could not launch it at once,
      // post-launch-paths plan section 3.7): an empty conversation over the composer, whose send launches it.
      return renderEmptyConversation("message-list-empty message-list-awaiting", renderOutgoingMessages(chatId));
    }
    const hasAccount = provisional.account_id !== "";
    const chatToAskId = chatToAskAboutFailedCreate(provisional);
    const sentence = failedCreateSentence(provisional);
    // What the page holds behind its "See details": the create's output, and a Try again's that failed again.
    const failedCreateDetails = joinDetailLines([provisional.error, launchError?.details ?? null]);
    // The failure as the page's Report a bug files it.
    const failedCreate: ReportedFailure = { message: sentence, details: failedCreateDetails };
    return m(
      "div",
      { class: "message-list-create-failed flex flex-col items-center justify-center h-full gap-4 p-8" },
      [
        ...renderFailedCreateHeldSends(chatId),
        m("p", { class: "type-heading text-primary" }, "This chat could not be started"),
        m("p", { class: "message-list-create-failed-text text-secondary text-center" }, sentence),
        m("div", { class: "text-secondary" }, m(SeeDetails, { details: provisional.error })),
        // A Try again that failed again is the page's own failure, which the page's remedies below are about.
        renderFailureLines(launchError, "message-list-create-retry-failed", true),
        renderFailureLines(pageActionError, "message-list-remove-failed"),
        m("div", { class: FAILED_CREATE_BUTTONS_CLASS }, [
          hasAccount
            ? [
                m(
                  Button,
                  {
                    variant: "primary",
                    extra: "message-list-create-retry",
                    readonly: launchInFlight,
                    disabled: askInFlight,
                    onclick: () => void relaunch(chatId, provisional.account_id),
                  },
                  launchInFlight ? "Starting…" : "Try again",
                ),
                m(
                  Button,
                  {
                    extra: "message-list-switch-provider",
                    readonly: launchInFlight,
                    disabled: askInFlight,
                    onclick: () => {
                      if (launchInFlight) return;
                      chooseProviderInPlaceOf(provisional.account_id, (accountId) => void launchOn(chatId, accountId));
                    },
                  },
                  SWITCH_PROVIDER_LABEL,
                ),
              ]
            : m(
                Button,
                {
                  variant: "primary",
                  extra: "message-list-choose-provider",
                  onclick: () => openProviderChooser({ onSignedIn: (accountId) => void launchOn(chatId, accountId) }),
                },
                "Choose a provider",
              ),
          m(
            Button,
            {
              extra: "message-list-remove-chat",
              readonly: pageActionInFlight,
              disabled: askInFlight,
              onclick: () => runPageAction(() => destroyChat(chatId), "This chat couldn't be removed"),
            },
            "Remove this chat",
          ),
          // The ask goes to a chat that is already running, and says under itself what a refused one came to,
          // where its bug report files the refusal with the create's failure. With no such chat, a new one would
          // meet what stopped this one, so Report a bug stands in the ask's place. A chat that names no account
          // needs a sign-in, which neither can make.
          chatToAskId !== null
            ? m(AskAgentToFixButton, {
                connection: pageAskConnection(chatToAskId),
                failure: {
                  whatFailed: `The chat "${provisional.name}" couldn't be started`,
                  details: failedCreateDetails,
                },
                reported: failedCreate,
              })
            : hasAccount
              ? m(ReportBugButton, {
                  connection: getShellConnection(),
                  failure: failedCreate,
                  extra: "message-list-create-report-bug",
                })
              : null,
        ]),
      ],
    );
  }

  /** Load the chat's transcript: the snapshot with the live stream, or the snapshot alone for a
   *  seeded chat that has no agent yet (the stream follows the chat's active agent, and its seed
   *  never changes; the reload once the first agent lands brings the stream). */
  async function loadChat(chatId: string, isStreamed = true): Promise<void> {
    try {
      if (isStreamed) {
        // Buffer SSE deltas arriving during the snapshot fetch so the wholesale
        // snapshot replace in fetchEvents cannot drop a live event on first load.
        await loadSnapshotWithStream(chatId);
      } else {
        await fetchEvents(chatId);
      }
    } catch (error) {
      // Where the load got to is recorded against the agent by `fetchEvents` and
      // read back in the view, so that a later attempt -- from any caller,
      // including the stream's own reconnect -- supersedes it. Nothing to hold
      // here, and nothing to guard on the agent having been switched away from:
      // the record is per-agent, so a stale load cannot speak for the new one.
      // Still logged, as the paging and reconnect paths do -- an attempt that a
      // newer one has superseded is recorded nowhere at all, so the log is the
      // only trace of one that keeps losing the race.
      console.warn(`Failed to load the transcript for chat ${chatId}`, error);
    }
  }

  // A user-initiated reload is outstanding; guards against stacking them.
  let reloadInFlight = false;

  /**
   * Re-run the load that the panel is currently reporting a failure for.
   *
   * Identical to what the window menu's Refresh does, offered where the user is
   * already looking: an error screen whose only remedy lives behind a menu they
   * have no particular reason to open reads as a dead end. Redraws on settle
   * because a *failed* reload writes only the load state, which no redraw
   * follows on its own (a successful one repaints when it places the window).
   */
  function reloadAfterFailure(chatId: string): void {
    if (reloadInFlight) {
      return;
    }
    reloadInFlight = true;
    loadChat(chatId).finally(() => {
      reloadInFlight = false;
      m.redraw();
    });
  }

  function manageStreamConnection(chatId: string): void {
    if (!isConversationNotFound(chatId)) {
      connectToStream(chatId);
    } else {
      disconnectFromStream(chatId);
    }
  }

  /** Load the chat once. */
  function ensureChatLoaded(chatId: string, isStreamed: boolean): void {
    if (chatId === currentChatId) {
      return;
    }

    currentChatId = chatId;
    // Resets all scroll state and loads this chat's persisted position (which
    // then steers the engine's fill toward it once the snapshot lands).
    engine.setChat(chatId);
    loadChat(chatId, isStreamed);
  }

  // A retry of the snapshot that 404'd is outstanding; only one at a time.
  let notFoundRetryInFlight = false;
  // A seeded chat this page showed as provisional: once the chat list names it (its first agent
  // landed), the transcript is reloaded so the window covers the new segment, and that load
  // connects the stream, which had no agent to follow while the chat was provisional.
  let seededChatAwaitingReload: string | null = null;

  function reloadSeededChatOnceRegistered(): void {
    const chatId = seededChatAwaitingReload;
    if (chatId === null || getChatById(chatId) === undefined) {
      return;
    }
    seededChatAwaitingReload = null;
    // The first send rode the agent's create and landed before this page had a stream to see
    // it arrive on, so the placed snapshot is what stands its "Sending…" bubble down.
    loadChat(chatId)
      .then(() => noteLoadedArrivals(chatId))
      .finally(() => m.redraw());
  }

  /**
   * Re-load a panel whose first events fetch 404'd, once the backend knows the
   * agent.
   *
   * A newly created chat lands in that window by construction: create-chat
   * returns 201 as soon as the background `mngr create` starts, and the agent is
   * only registered when that finishes, so the panel's first fetch races ahead
   * of it. `fetchEvents` latches the miss and only ever clears it on its own next
   * call, which `ensureChatLoaded` never makes for an agent it has already
   * loaded -- so without this the panel sits on "No conversation data" until the
   * page is reloaded.
   *
   * The trigger is the `chats_updated` snapshot rather than a retry timer, and
   * it cannot spin: `/events` resolves the chat through the same list that
   * feeds `chats_updated`, so the chat being named here is exactly the
   * condition under which the refetch stops 404ing.
   */
  function retryAfterChatResolved(): void {
    const chatId = currentChatId;
    if (chatId === null || notFoundRetryInFlight || !isConversationNotFound(chatId)) {
      return;
    }
    // Read the chat store rather than the listener's payload, so the retry does not
    // depend on which push woke it.
    if (getChatById(chatId) === undefined) {
      return;
    }
    notFoundRetryInFlight = true;
    loadChat(chatId).finally(() => {
      notFoundRetryInFlight = false;
      m.redraw();
    });
  }

  /** Whether a transcript 404 is the answer rather than a race. A new chat's page can load before
   *  the chat app has told it about the chat, and the reload that follows the chat coming up is
   *  still in flight when it does: both are the chat not known yet, which reads as an empty chat.
   *  Only a chat the app lists (or has no record of) with no retry pending has no conversation. */
  function isNotFoundSettled(chatId: string): boolean {
    return hasReceivedChatList() && !(notFoundRetryInFlight && currentChatId === chatId);
  }

  /** A chat whose conversation the chat app does not have: gone, stopped, or with nothing in it yet. The agent's
   *  terminal screen, and why it could not be read, are behind See details. */
  function renderNothingToShow(chatId: string): m.Vnode {
    const agent = getChatById(chatId)?.active_agent;
    const screenDetails = screenLoading
      ? null
      : joinDetailLines([
          screenContent,
          screenError === null ? null : `Could not read the agent's terminal: ${screenError}`,
        ]);
    const isGone = agent === undefined;
    const isStopped = agent !== undefined && isAgentProcessDead(agent.state);
    return m("div", { class: "message-list-not-found flex flex-col items-center justify-center h-full gap-4 p-8" }, [
      m(
        "p",
        { class: "message-list-not-found-text text-secondary text-center" },
        isGone
          ? "This chat is no longer here."
          : isStopped
            ? "This chat has nothing to show yet -- its agent isn't running."
            : "This chat has nothing to show yet.",
      ),
      isGone
        ? m(
            Button,
            { sm: true, extra: "message-list-new-chat", onclick: () => startNewChatInPlaceOf(chatId) },
            "Start a new chat",
          )
        : isStopped
          ? m(
              Button,
              {
                sm: true,
                extra: "message-list-start-chat",
                readonly: pageActionInFlight,
                onclick: () => runPageAction(() => startChat(chatId), "This chat couldn't be started"),
              },
              pageActionInFlight ? "Starting…" : "Start this chat",
            )
          : null,
      renderFailureLines(pageActionError, "message-list-start-failed"),
      m("div", { class: "text-secondary" }, m(SeeDetails, { details: screenDetails })),
    ]);
  }

  function renderMessages(chatId: string): m.Vnode {
    // A provisional record short-circuits the load: there is no agent to read yet. A load that
    // raced ahead of the record (a page opened before the socket replayed it) 404s and latches
    // not-found until the agent registers, which retries it (retryAfterChatResolved). A seeded
    // chat is the exception: its seed segment reads like any transcript, so the page loads it.
    const provisional = provisionalRecord(chatId);
    if (provisional !== null && !isSeededTranscriptShown(provisional)) {
      return renderProvisional(chatId, provisional);
    }
    if (provisional !== null) {
      // The seed is read once and never changes, and the stream needs an active agent, which
      // the chat has none of until its first send launches one: no stream until then.
      seededChatAwaitingReload = chatId;
      ensureChatLoaded(chatId, false);
    } else {
      ensureChatLoaded(chatId, true);
      manageStreamConnection(chatId);
    }

    if (isConversationNotFound(chatId) && !isNotFoundSettled(chatId)) {
      return renderWhileLoading(chatId);
    }
    if (isConversationNotFound(chatId)) {
      fetchScreenCapture(chatId);
      return renderNothingToShow(chatId);
    }

    // A message the user just sent counts as content even before any event
    // exists for it: it may be queued (the harness parked it) or still in flight
    // (an optimistic "Sending…" bubble). All three whole-panel states below are
    // about having nothing to show, so they share one answer -- otherwise a
    // reload firing under a fresh chat replaces that bubble with a spinner or an
    // error screen.
    const tailNodes =
      getEventCount(chatId) === 0
        ? [
            ...asAgentTurn([renderHandoffTailNode(chatId, false)].filter((node) => node !== null)),
            ...renderPendingSends(chatId, closedPendingRun()),
          ].filter((node) => node !== null)
        : [];
    const hasNothingToShow = getEventCount(chatId) === 0 && tailNodes.length === 0;

    // Read per-render rather than latched at load time, so the panel leaves the
    // error state as soon as any reload succeeds -- the window menu's Refresh or the
    // stream's background reconnect, neither of which goes through loadChat.
    // The phase, not just the error: a load that is in flight -- including a retry -- must not
    // fall through to the empty state, which claims an answer it does not have.
    const load = getConversationLoadState(chatId);
    if (hasNothingToShow && load.phase === "loading") {
      return renderWhileLoading(chatId);
    }

    if (hasNothingToShow && load.error !== null) {
      const lines = plainFailureLinesOf(load.error, "This conversation couldn't be loaded", { requestKind: "read" });
      return m("div", { class: "message-list-error flex flex-col items-center justify-center h-full gap-3 p-8" }, [
        m("p", { class: "message-list-error-text text-danger text-center" }, plainFailureSentence(lines)),
        m("div", { class: "flex flex-wrap items-center justify-center gap-2" }, [
          m(Button, { sm: true, extra: "message-list-reload", onclick: () => reloadAfterFailure(chatId) }, "Refresh"),
          m(FailureRemedies, { connection: getShellConnection(), failure: shownFailureOfLines(lines), sm: true }),
        ]),
        m("div", { class: "text-secondary" }, m(SeeDetails, { details: lines.details })),
      ]);
    }

    // The same failure, over a transcript that is already on screen. Keeping the
    // transcript is right -- blanking it loses more than the error tells -- but
    // staying silent is not: the user may have just asked for this reload
    // themselves, and got no answer either way. So it reports as a strip above the
    // transcript rather than in place of it, carrying the same retry the error
    // screen offers.
    const failedReloadNotice =
      load.error === null
        ? null
        : m(
            "div",
            {
              class:
                "message-list-stale-notice flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-default px-3 py-1.5",
            },
            [
              m(
                "span",
                { class: "text-sm text-danger" },
                "Couldn't refresh this conversation -- what you see may be out of date.",
              ),
              m(
                Button,
                { sm: true, extra: "message-list-reload", onclick: () => reloadAfterFailure(chatId) },
                "Refresh",
              ),
              m(
                "div",
                { class: "basis-full text-secondary" },
                m(SeeDetails, {
                  details: joinDetailLines([
                    load.error.cause === "user_facing" ? load.error.userFacingMessage : null,
                    load.error.details,
                  ]),
                }),
              ),
            ],
          );

    const events = getEventsForChat(chatId);

    if (events.length === 0) {
      if (tailNodes.length === 0) {
        // An empty chat can still be busy (a compaction, a switch, a background task): the status row
        // says so in place of the ready line.
        const nodes = isAgentBusy(chatId) ? turnStatusNodes(chatId, events) : [];
        return renderEmptyConversation("message-list-empty", nodes, READY_STATUS);
      }
      return m("div", { class: "message-list-wrapper" }, [
        failedReloadNotice,
        m("div", { class: MESSAGE_LIST_CLASS }, [...tailNodes, ...turnStatusNodes(chatId, events)]),
      ]);
    }

    const chat = getChatById(chatId);
    const activityState = chat?.active_agent.activity_state;
    const agentIsIdle = activityState === "IDLE";
    const tailTurnSettled = isTailTurnSettled(activityState);
    const liveCompaction = liveCompactionOf(activityState, chat?.active_agent.compaction_cause);

    // A new chat starts on fast mode; once it has run the workspace's turn limit,
    // switch it to standard speed. Checked here because this is where the loaded
    // transcript and the idle flag meet.
    maybeApplyFastModeLimit(chat, events, agentIsIdle);

    // Memoize the turn-grouping -> rows pipeline. buildSections walks the entire
    // held transcript, so recomputing it on every scroll-driven redraw is the
    // dominant scroll cost on a long conversation. Its output depends only on the
    // held events, the settled flag and the running compaction -- captured by the
    // render version (bumped on any data mutation) plus those two -- so a
    // scroll-only redraw reuses the cached rows.
    const renderKey =
      `${chatId}|${getRenderVersion(chatId)}|${getResentSendsKey(chatId)}|${tailTurnSettled ? 1 : 0}|` +
      `${liveCompaction === null ? "-" : (liveCompaction.cause ?? "unknown")}`;
    if (renderKey !== rowsCacheKey) {
      // Both structure and decoration come from the transcript walk; there is no
      // side-channel enrichment. The same pipeline feeds the subagent view, so a
      // subagent's "View conversation" renders an identical progress timeline.
      if (liveCompaction === null) closeRunningCompactionChips();
      const view = buildConversationView(
        chatId,
        events,
        tailTurnSettled,
        liveCompaction,
        resentTurnsIn(chatId, events),
        () => liveReplyDraft(chatId),
        () =>
          hasMoreAfter(chatId)
            ? []
            : [
                ...renderAgentLiveTail(chatId, false, isReplyDraftInStep),
                ...stretchStatusNodes(chatId, getEventsForChat(chatId), isRunningCallInline(chatId, cachedRows)),
              ],
        (eventIds) => answeredReplySuggestions(chatId, eventIds),
      );
      cachedRows = view.rows;
      isReplyDraftInStep = view.isReplyDraftInStep;
      rowsCacheKey = renderKey;
    }
    const rows = cachedRows;

    // The engine decides everything about what mounts: the virtual end spacers,
    // the visible row window (viewport + overscan, grown while a selection is
    // live), and -- in afterRender -- where the viewport sits. Rendered as
    // spacer / row-run / spacer via the shared segment renderer.
    const plan = engine.computeRenderPlan();
    const isTurnStatusAfterSends = turnStatusPlacement(chatId) === "after-sends";
    const liveEnd = renderLiveEndAfterRows(chatId, events, rows);
    return m("div", { class: "message-list-wrapper" }, [
      failedReloadNotice,
      // The queued-message group renders after the virtualized rows so it sits at
      // the live tail, below the last committed turn. It is a full snapshot from
      // the harness, replaced wholesale on each push.
      m("div", { class: MESSAGE_LIST_CLASS }, [
        ...renderTranscriptSegments(rows, [
          { kind: "spacer", height: plan.topPadPx },
          { kind: "rows", startIndex: plan.startIndex, endIndex: plan.endIndex },
          { kind: "spacer", height: plan.bottomPadPx },
        ]),
        // The agent's output past the transcript and the status row end the last row's stretch when
        // that row is the agent's and ends the transcript; otherwise they follow it.
        ...liveEnd.nodes,
        // The latest turn's done mark once it has finished; anything queued behind it follows, then
        // the messages the chat app holds while the chat switches harness, then this page's own
        // not-yet-delivered sends.
        m(TurnDoneMark, { key: "turn-done", chatId, events }),
        ...renderPendingSends(chatId, pendingRunAfterRows(chatId, rows, liveEnd)),
        // The status row under the user's own send while it waits on the agent or opens the turn the row reports.
        ...(isTurnStatusAfterSends ? turnStatusNodes(chatId, events) : []),
      ]),
    ]);
  }

  const handleChatsUpdated = (): void => {
    retryAfterChatResolved();
    reloadSeededChatOnceRegistered();
  };

  const handleMessageSent = (chatId: string): void => {
    if (chatId === currentChatId) {
      engine.noteMessageSent();
    }
  };

  return {
    oninit() {
      addChatsUpdatedListener(handleChatsUpdated);
      addMessageSentListener(handleMessageSent);
    },

    onremove() {
      removeChatsUpdatedListener(handleChatsUpdated);
      removeMessageSentListener(handleMessageSent);
      engine.detach();
      if (currentChatId !== null) {
        disconnectFromStream(currentChatId);
      }
    },

    view(vnode) {
      const chatId = vnode.attrs.chatId;
      const isCompact = vnode.attrs.isCompact === true;
      // The terminal back face attaches to the agent's own tmux session, which a chat still being
      // created does not have: without a name the terminal dispatch attaches to whatever session
      // it finds, so the flip waits for the agent to register.
      const isRoom = isRoomChat(chatId);
      const isListed = getChatById(chatId) !== undefined;
      // The shell's live visibility for this frame, fed in by the page. Read
      // it before building content / running lifecycle hooks so the scroll hooks
      // (which read this closure variable) see the current value. Undefined for a
      // mount without a panel api -- treat that as visible.
      panelVisible = vnode.attrs.isVisible ?? true;

      const content = isSlotClaimed("conversation-content") ? null : renderMessages(chatId);

      const acceptsFileDrops = !isConversationNotFound(chatId);

      // The two renderings of one conversation. `hasEverFlipped` is STICKY and separate from
      // `isFlipped` on purpose: mithril destroys a vnode that becomes null, and destroying the
      // back face takes its iframe out of the document -- which ends the ttyd session rather
      // than hiding it. So the back face mounts on the first flip and stays mounted forever;
      // only the transform changes after that.
      if (isFlipped) hasEverFlipped = true;

      return m(
        "div",
        {
          class: "chat-panel flex flex-col h-full relative",
          oncreate: (panelVnode: m.VnodeDOM) => {
            panelElement = panelVnode.dom as HTMLElement;
          },
          ondragenter: acceptsFileDrops ? handleDragEnter : undefined,
          ondragover: acceptsFileDrops ? handleDragOver : undefined,
          ondragleave: acceptsFileDrops ? handleDragLeave : undefined,
          ondrop: acceptsFileDrops ? (event: DragEvent) => handleDrop(event, chatId) : undefined,
        },
        [
          isFileDragActive && acceptsFileDrops
            ? m(
                "div",
                {
                  // z-50: design-system-exception -- a mid-layer overlay above
                  // chat content but below the modal stack; the z scale has no
                  // name for it.
                  class:
                    "chat-drop-overlay absolute inset-0 z-50 m-2 flex items-center justify-center rounded-lg " +
                    "border-2 border-dashed border-accent bg-accent-light/70 pointer-events-none",
                },
                m(
                  "div",
                  {
                    class:
                      "chat-drop-overlay-label rounded-full border border-accent bg-surface px-4.5 py-2.5 " +
                      "text-(length:--font-size-body) font-medium text-accent shadow-overlay",
                  },
                  "Drop files to attach",
                ),
              )
            : null,
          chatFlipCard({
            flipped: isFlipped,
            everFlipped: hasEverFlipped,
            back: () =>
              m(AgentTerminalPanel, {
                chatId,
                url: getChatTerminalUrl(chatId),
                title: `${getChatById(chatId)?.active_agent.name ?? "agent"} terminal`,
              }),
            front: [
              // The transcript area: the scroll container (native scrolling, native
              // scrollbar hidden), the custom overlay scrollbar, and the
              // loading-overlay for when the viewport sits over a virtual end spacer.
              m("div", { class: "chat-transcript-area relative flex-1 min-h-0 flex flex-col" }, [
                m(
                  "main",
                  {
                    // A column, so the suggestion layer that ends it sits at its bottom edge under a short
                    // transcript too. No padding at its end: what the transcript ends on brings its own margin,
                    // which is all that stands between it and the footer's line or the first pill.
                    class:
                      "app-content transcript-scroll flex flex-1 flex-col overflow-y-auto bg-chat *:shrink-0 " +
                      (isCompact ? "px-4 pt-4" : "px-6 pt-6"),
                    // Focusable so native keyboard scrolling (PageUp/Down, Home/End)
                    // works; the engine's listeners classify the input source.
                    tabindex: 0,
                    oncreate: (mainVnode: m.VnodeDOM) => {
                      engine.afterRender(mainVnode.dom as HTMLElement);
                      reportChatOnScreenOnce(chatId, isTranscriptLoaded(chatId));
                    },
                    onupdate: (mainVnode: m.VnodeDOM) => {
                      engine.afterRender(mainVnode.dom as HTMLElement);
                      reportChatOnScreenOnce(chatId, isTranscriptLoaded(chatId));
                    },
                  },
                  [
                    content,
                    m(SuggestionLayer, {
                      chatId,
                      onElement: (element) => {
                        suggestionLayerElement = element;
                      },
                    }),
                  ],
                ),
                m(TranscriptScrollbar, { engine }),
                // While the viewport is over a virtual end spacer (e.g. the scrollbar
                // was dragged into not-yet-loaded history), overlay a loading indicator
                // so the user never sees a blank area. pointer-events:none so it never
                // blocks scroll.
                engine.isViewportInSpacer()
                  ? m(
                      "div",
                      {
                        class:
                          "message-list-window-loading absolute inset-0 flex items-center justify-center p-6 pointer-events-none",
                      },
                      m("p", { class: "text-secondary" }, "Loading messages..."),
                    )
                  : null,
              ]),
              // Present for every chat, a create in flight included: a message typed while the
              // chat is being created is held and delivered when it lands.
              m("footer", { class: `app-footer shrink-0 bg-chat ${isCompact ? "px-3" : "px-6"}` }, [
                m(EmptySlot, { name: "conversation-before-input" }),
                // The switch dialog a provider choice opens (spec 5.1), and why a switch
                // failed, with a retry on any account (spec 5.10).
                m(SwitchDialog, { chatId }),
                m(HandoffFailedNotice, { chatId }),
                m(AgentWorkingReport, { chatId }),
                m(ChatSuggestions, { chatId }),
                m(MessageInput, {
                  chatId,
                  onTurnOnSourceView: isListed ? turnOnSourceView : undefined,
                  leading:
                    isCompact && isListed
                      ? m(ModelProviderMenu, {
                          chatId,
                          isCompact: true,
                          sourceView: { on: isFlipped, onToggle: toggleSourceView },
                        })
                      : undefined,
                }),
                // The under-bar is a sibling of the whole flip card, not part of this face: on
                // a face it would rotate away with the face its own switch turns, and the flip
                // would be one-way.
              ]),
            ],
          }),
          // OUTSIDE the flip. Inside, the switch would rotate away with the face it turns and
          // the flip would be one-way. Everything here describes the conversation rather than
          // either rendering of it, which is the same reason it belongs to neither face.
          // An 8px band under the under-bar, matching the 8px above it: the row is a caption
          // on the composer and sits evenly in its own band.
          m(
            "div",
            { class: `chat-under-bar shrink-0 bg-chat pb-2 ${isCompact ? "px-3" : "px-6"}` },
            m(
              "div",
              {
                // Same max-width as the composer card above it; relative as
                // the containing block for centered overlays. As tall as the model chip
                // whether or not anything in it has rendered yet, so the composer never moves;
                // on a phone, where the chip is the settings button, only as tall as a notice in it.
                class:
                  `composer-under-bar relative mx-auto mt-1 flex w-full ${isCompact ? "" : "min-h-[30px]"} ` +
                  "max-w-(--width-message-column) items-center gap-2",
              },
              [
                // On a phone a chat with no agent yet still names its account here: it has no menu to open.
                isCompact && isListed ? null : m(ModelProviderMenu, { chatId }),
                m(FastModeNotice, { chatId }),
                !isListed || isRoom || (isCompact && !isFlipped)
                  ? null
                  : m("div", { class: "composer-under-bar-actions ml-auto flex items-center gap-0.5" }, [
                      m(TerminalViewToggle, { on: isFlipped, onToggle: toggleSourceView }),
                    ]),
              ],
            ),
          ),
        ],
      );
    },
  };
}
