/**
 * The status row at the live end of the transcript -- the harness-common shell. It sits where
 * the reply will appear, so it is where the user is already looking once they send: just under
 * their own message while that waits on the agent, and under the latest reply while the agent
 * works (``turnStatusPlacement`` says which).
 *
 * The backend (the chat app) is the source of truth for *which* state the agent
 * is in -- IDLE / THINKING / TOOL_RUNNING / COMPACTING -- delivered on ``activity_state`` via
 * the ``chats_updated`` WS payload. This component's job is to render a label:
 *   - IDLE / null      -> hidden
 *   - THINKING         -> "Thinking…"
 *   - TOOL_RUNNING     -> the in-flight tool call, captioned by the agent's harness
 *   - COMPACTING       -> "Compacting…", named for who started it ("Compacting while idle…"),
 *                         and "…, then replying…" once a message is queued behind it
 * and, once a turn has run for `ELAPSED_SHOWN_AFTER_MS`, how long it has run so far: from the
 * moment the user sent the message that opened it when this page sent it, so the count runs on
 * unbroken from the send's own, else from the transcript's record of when the turn started
 * (`turnTiming`).
 *
 * Before the agent's turn starts, the row stands for the user's send (contract A1/A2): a send
 * waiting on the agent to come up says "Connecting to <model> on <provider>…" (the backend's
 * ``is_connecting``), and any other send still in flight shows the row's dot at once, captioned
 * "Sending to <harness>…" only if it is still in flight after `SENDING_LABEL_AFTER_MS`. Both
 * are the Sending state drawn on the agent's side; neither resolves anything, and both give way
 * as soon as the backend reports the turn. Once the agent's turn ends the row goes, though its
 * harness may still be closing it (Claude's Stop hooks): the reply is in, so the done mark under
 * it (`TurnDoneMark`) takes over at once.
 *
 * The TOOL_RUNNING caption is read straight off the tool call: the harness's own
 * parser labelled it, so this view needs no notion of which harness is running.
 * A null ``activity_state`` means the server has no per-agent activity tracking
 * for this chat (provisional chats, remote agents) -- the strip collapses.
 *
 * One state has no backend signal at all: the beat between the user resolving a
 * permission request and the agent being told. The verdict reaches this page
 * over the embed contract the moment the user clicks (see `permission-card.ts`),
 * but the agent only resumes once the desktop client's retried `mngr message`
 * delivery actually lands the notice in its session -- until then the agent is
 * genuinely IDLE and the strip would show nothing, so the user sees their
 * decision land and then apparent silence. Through that gap the strip says
 * `WAKE_LABEL`, bounded by `WAKE_SPINNER_MS`.
 *
 * Above the activity line, whatever it says, sits the wait line (`BackgroundTasksLine`) while
 * the chat has background tasks pending: the agent may be between turns, but one of them will
 * start the next.
 */

import m from "mithril";
import { activityDotClass, type ActivityDotTone } from "@imbue/workspace-ui/src/components/activityDot";
import type { CompactionCause, ToolCall, TranscriptEvent } from "../models/Response";
import { COMPACTING_STATE } from "../models/activityState";
import { getChatById, getQueuedMessagesForChat } from "../models/Chats";
import { getOutgoingMessages, sentAtOfArrival } from "../models/OutgoingMessages";
import { BackgroundTasksLine } from "./BackgroundTasksLine";
import { connectingLabel, isChatConnecting } from "./ConnectingIndicator";
import { handoffPhaseText } from "./handoff-phase";
import { harnessLabel } from "./harness-labels";
import { resolutionRequestIdOf } from "./message-classification";
import { hasShellResolutionSince, shellResolutionArrivalFor } from "./permission-card";
import { formatElapsed, latestTurnSpan, type TurnSpan } from "./turnTiming";

/**
 * Find the most recent assistant tool call whose tool_call_id has no matching
 * tool_result event. Returns null if none. (Harness-agnostic: both parsers emit the
 * same ``assistant_message`` / ``tool_result`` shape.)
 */
function pendingToolCall(events: TranscriptEvent[]): ToolCall | null {
  const resolved = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "tool_result" && e.tool_call_id) {
      resolved.add(e.tool_call_id);
      continue;
    }
    if (e.type === "assistant_message" && e.tool_calls && e.tool_calls.length > 0) {
      for (let j = e.tool_calls.length - 1; j >= 0; j--) {
        const tc = e.tool_calls[j];
        if (!resolved.has(tc.tool_call_id)) {
          return tc;
        }
      }
    }
  }
  return null;
}

/** The in-flight tool caption, as labelled by the harness that produced the call. */
function labelForToolCall(tc: ToolCall): string {
  return tc.caption_label || "Running tool…";
}

function compactingLead(cause: CompactionCause | null): string {
  switch (cause) {
    case "manual":
      return "Compacting as requested";
    case "idle":
      return "Compacting while idle";
    case "native":
      return "Compacting to free up context";
    default:
      return "Compacting";
  }
}

/** What a running compaction is called, on the strip and on the transcript's chip for its start. */
export function compactingLabel(cause: CompactionCause | null, hasQueuedMessages: boolean): string {
  const lead = compactingLead(cause);
  return hasQueuedMessages ? `${lead}, then replying…` : `${lead}…`;
}

/**
 * Pick the user-facing label for a server-derived activity state. For TOOL_RUNNING
 * we consult the transcript for the in-flight tool and use its label; COMPACTING
 * says who started it and whether a queued message waits on it; every other state is fixed
 * (or null = hide).
 */
export function labelForActivityState(
  state: string | null | undefined,
  events: TranscriptEvent[],
  hasQueuedMessages = false,
  compactionCause: CompactionCause | null = null,
): string | null {
  if (state === null || state === undefined) return null;
  if (state === "IDLE") return null;
  if (state === "THINKING") return "Thinking…";
  if (state === COMPACTING_STATE) return compactingLabel(compactionCause, hasQueuedMessages);
  if (state === "TOOL_RUNNING") {
    const pending = pendingToolCall(events);
    if (pending !== null) return labelForToolCall(pending);
    return "Running tool…";
  }
  return null;
}

// What the strip says while the verdict is on its way to the agent. Phrased
// around the permission change rather than around the agent, because at this
// point nothing is known about what the agent will do with it -- claiming it is
// "thinking" would be inventing detail, and it may yet never be told at all.
const WAKE_LABEL = "Confirming permission changes…";

// Marks the wake-up strip for styling and tests. Deliberately not one of the
// server's activity states -- the server has no signal for this beat.
const WAKE_STATE = "WAKING";

// The row's place in the message column it sits in, spaced like a message so the reply that
// replaces it does not move what is above it.
const STRIP_COLUMN_CLASS = "mb-5 w-full";

// How long a turn (or a send waiting on the agent) runs before the row says how long it has run:
// a quick answer needs no clock, a long one should show that it is still moving.
export const ELAPSED_SHOWN_AFTER_MS = 3_000;

// How long a send can be in flight with only the dot before the row says where it is: most
// deliveries land before this, and a caption that flashes past is noise.
export const SENDING_LABEL_AFTER_MS = 1_500;

// Marks the row of a send still on its way to the agent, for styling and tests. Like the wake-up
// state, not one of the server's activity states: the row stands for the user's send.
const SENDING_STATE = "SENDING";
// The row of a send waiting for the agent to come up (``is_connecting``).
const CONNECTING_STATE = "CONNECTING";
// The row of a turn whose message is in the transcript but which the agent has not reported working on.
const STARTING_STATE = "STARTING";

function renderStrip(
  label: string | null,
  state: string | null | undefined,
  columnClass: string,
  elapsed: string | null = null,
  dotTone: ActivityDotTone = "accent",
): m.Vnode {
  // The dot's pulse keyframes (agent-activity-pulse) live in the library's base.css.
  return m(
    "div",
    {
      class:
        `agent-activity-indicator ${columnClass} flex min-h-5 items-center gap-2 px-1 ` +
        "text-(length:--font-size-helper) text-secondary",
      "data-state": state,
      role: "status",
      "aria-live": "polite",
    },
    [
      m("span", {
        class: `agent-activity-indicator__dot ${activityDotClass("h-1.5 w-1.5", dotTone)}`,
      }),
      label === null ? null : m("span", { class: "agent-activity-indicator__label truncate" }, label),
      elapsed === null
        ? null
        : m("span", { class: "agent-activity-indicator__elapsed shrink-0 tabular-nums text-faint" }, elapsed),
    ],
  );
}

/** How long something running since `startedAt` has run, once that is long enough to be worth saying. */
function elapsedSince(startedAt: number | null, now: number): string | null {
  if (startedAt === null || now - startedAt < ELAPSED_SHOWN_AFTER_MS) return null;
  return formatElapsed(now - startedAt);
}

// Per chat, the message that opened the last turn this page saw settle. The backend can report a new
// turn running a moment before that turn's own message reaches the transcript; until it does, the
// latest message there is the finished turn's, which is no start to count from.
const settledTurnByChat = new Map<string, string>();

/** When the turn opened by the transcript's latest turn-opening message started: when the user sent
 *  that message, if this page sent it, else when the transcript recorded it. */
export function turnStartedAt(span: TurnSpan): number {
  return sentAtOfArrival(span.openingEventId) ?? span.startedAt;
}

/** When the turn the row reports started, or null when the row reports none or its message has not
 *  landed yet. Notes the latest turn as settled once the agent is idle after working on it. */
function runningTurnStartedAt(
  chatId: string,
  state: string | null,
  isTurnReported: boolean,
  events: readonly TranscriptEvent[],
): number | null {
  const span = latestTurnSpan(events);
  if (span === null) return null;
  if (state === "IDLE") {
    if (span.hasAgentWork) settledTurnByChat.set(chatId, span.openingEventId);
    return null;
  }
  return isTurnReported && settledTurnByChat.get(chatId) !== span.openingEventId ? turnStartedAt(span) : null;
}

/** Forget what the page has seen of each chat's turns. */
export function resetSettledTurnsForTesting(): void {
  settledTurnByChat.clear();
}

/** When the chat's oldest send still on its way was made, or null when none is. */
function oldestSendingAt(chatId: string): number | null {
  const outgoing = getOutgoingMessages(chatId);
  return outgoing.length === 0 ? null : Math.min(...outgoing.map((message) => message.sentAt));
}

/** Where the status row goes at the live end of the transcript: under the user's own sends while it
 *  stands for one (a send on its way, or waiting on the agent to come up), else under the latest
 *  turn, ahead of anything queued behind it. */
export function turnStatusPlacement(chatId: string): "after-sends" | "after-turn" {
  const chat = getChatById(chatId);
  const isWorking =
    chat !== undefined &&
    (chat.handoff !== null ||
      (chat.active_agent.activity_state !== null && chat.active_agent.activity_state !== "IDLE"));
  if (isWorking) return "after-turn";
  return isChatConnecting(chatId) || getOutgoingMessages(chatId).length > 0 ? "after-sends" : "after-turn";
}

// How long the wake-up strip may stand in for the agent after a verdict. Long
// enough to cover an ordinary delivery (discovery, the TUI paste-and-confirm
// handshake, and a retry or two), short enough that a delivery which never
// lands leaves the chat honestly idle rather than spinning forever.
const WAKE_SPINNER_MS = 20_000;

/**
 * When the wake-up strip should stop, or null when it should not be shown.
 *
 * It is shown for a request meeting all three of:
 *   (a) THIS agent filed it -- verdicts are recorded page-wide, so a sibling
 *       panel's request must not spin this panel;
 *   (b) the shell reported its verdict within the last `WAKE_SPINNER_MS`;
 *   (c) the transcript does not yet carry its resolution notice -- i.e. the
 *       agent still has not been told.
 *
 * (c) is what keeps a reloaded page quiet: the load-time snapshot re-reports
 * verdicts the agent heard about long ago, and every one of those already has
 * its notice in the transcript.
 *
 * Returns the latest such deadline, so a second verdict resolved during the
 * window extends the strip rather than truncating it.
 */
export function wakeUpSpinnerDeadline(events: TranscriptEvent[], now: number): number | null {
  const since = now - WAKE_SPINNER_MS;
  // Cheap gate: no verdict landed recently, so no scan is worth doing.
  if (!hasShellResolutionSince(since)) return null;

  const filedHere = new Set<string>();
  const announcedHere = new Set<string>();
  for (const e of events) {
    if (e.type === "tool_result") {
      // The backend parses the gateway's echoed object off the untruncated tool
      // output onto `permission_request`; its `request_id` is the same id the
      // shell's verdicts are keyed by.
      const requestId = e.permission_request?.request_id;
      if (typeof requestId === "string" && requestId !== "") filedHere.add(requestId);
      continue;
    }
    if (e.type === "user_message") {
      const requestId = resolutionRequestIdOf(e);
      if (requestId !== null) announcedHere.add(requestId);
    }
  }

  let deadline: number | null = null;
  for (const requestId of filedHere) {
    if (announcedHere.has(requestId)) continue;
    const arrival = shellResolutionArrivalFor(requestId);
    if (arrival === null || arrival <= since) continue;
    const candidate = arrival + WAKE_SPINNER_MS;
    if (deadline === null || candidate > deadline) deadline = candidate;
  }
  return deadline;
}

// Minimum time a "Running X" caption stays up. A tool call that finishes in a
// fraction of a second would otherwise flash past unreadably -- common for codex,
// whose code-mode calls often yield immediately, but claude has fast tools too, so
// this is about tool duration rather than about which harness is running. The hold
// applies ONLY while the agent is still working (THINKING); the turn ending (IDLE)
// clears it at once, so the indicator never lingers past when it should go away.
const TOOL_CAPTION_MIN_MS = 700;

interface ActivityIndicatorAttrs {
  chatId: string;
  events: TranscriptEvent[];
}

export function ActivityIndicator(): m.Component<ActivityIndicatorAttrs> {
  // Per-mounted-panel (i.e. per-agent) debounce state for the tool caption.
  let heldToolCaption: string | null = null;
  let heldUntil = 0;
  let releaseTimer: number | null = null;
  // Fires at the wake-up strip's deadline: nothing else would redraw this panel
  // when the window simply runs out (the agent never arriving is, by
  // definition, the absence of an event).
  let wakeTimer: number | null = null;

  const cancelRelease = (): void => {
    if (releaseTimer !== null) {
      window.clearTimeout(releaseTimer);
      releaseTimer = null;
    }
  };

  const cancelWake = (): void => {
    if (wakeTimer !== null) {
      window.clearTimeout(wakeTimer);
      wakeTimer = null;
    }
  };

  // Redraws the row when what it says next depends only on the clock: the elapsed count's next
  // second, or the moment a send in flight earns its caption. Nothing else would redraw it then.
  let tickTimer: number | null = null;

  const cancelTick = (): void => {
    if (tickTimer !== null) {
      window.clearTimeout(tickTimer);
      tickTimer = null;
    }
  };

  /** Redraw at `at`, unless a redraw is already due sooner. */
  const scheduleRedrawAt = (at: number, now: number): void => {
    if (tickTimer !== null) return;
    tickTimer = window.setTimeout(
      () => {
        tickTimer = null;
        m.redraw();
      },
      Math.max(50, at - now),
    );
  };

  /** Redraw once the clock next changes what a row running since `startedAt` says: its elapsed count's
   *  next second (or the moment it is first shown), or `captionAt` when that comes first. */
  const scheduleTick = (startedAt: number, now: number, captionAt: number | null = null): void => {
    const ranFor = now - startedAt;
    const nextElapsedAt =
      ranFor < ELAPSED_SHOWN_AFTER_MS ? startedAt + ELAPSED_SHOWN_AFTER_MS : now + 1000 - (Math.max(0, ranFor) % 1000);
    scheduleRedrawAt(captionAt === null || captionAt <= now ? nextElapsedAt : Math.min(captionAt, nextElapsedAt), now);
  };

  /** The row of a send not yet in a turn: waiting on the agent to come up, or still on its way. Null for neither. */
  const sendStrip = (chatId: string, columnClass: string, now: number): m.Vnode | null => {
    const sentAt = oldestSendingAt(chatId);
    if (isChatConnecting(chatId)) {
      if (sentAt !== null) scheduleTick(sentAt, now);
      // The amber dot: the agent is not up yet, which is a wait of its own rather than work.
      return renderStrip(connectingLabel(chatId), CONNECTING_STATE, columnClass, elapsedSince(sentAt, now), "warning");
    }
    if (sentAt === null) return null;
    const captionAt = sentAt + SENDING_LABEL_AFTER_MS;
    scheduleTick(sentAt, now, captionAt);
    const harness = getChatById(chatId)?.active_agent.harness;
    const caption =
      now < captionAt ? null : harness === undefined ? "Sending…" : `Sending to ${harnessLabel(harness)}…`;
    return renderStrip(caption, SENDING_STATE, columnClass, elapsedSince(sentAt, now));
  };

  /** The row of a turn whose message has reached the transcript while the backend reports the chat
   *  working but not yet its activity: the dot, with the count the send started. Null for any other
   *  chat, including one whose agent has already worked on the latest turn (it is closing, and the
   *  done mark says so). */
  const startingStrip = (attrs: ActivityIndicatorAttrs, columnClass: string, now: number): m.Vnode | null => {
    const chat = getChatById(attrs.chatId);
    if (chat === undefined || chat.status !== "working" || chat.active_agent.activity_state !== "IDLE") return null;
    const span = latestTurnSpan(attrs.events);
    if (span === null || span.hasAgentWork) return null;
    const startedAt = turnStartedAt(span);
    scheduleTick(startedAt, now);
    return renderStrip(null, STARTING_STATE, columnClass, elapsedSince(startedAt, now));
  };

  /** The activity line: the switch's phase, the turn's caption, the wake-up caption, the row of a send
   *  not yet in a turn, or the row of a turn not yet started; null when there is none. */
  const activityStrip = (attrs: ActivityIndicatorAttrs, columnClass: string): m.Vnode | null => {
    cancelTick();
    const { chatId, events } = attrs;
    const chat = getChatById(chatId);
    // A chat switching harness reports the switch, not the retiring agent's turn: that
    // agent is busy with the summary it was asked for, which is the switch's own business.
    // The failed phase has its own notice over the composer and shows nothing here.
    if (chat !== undefined && chat.handoff !== null && chat.handoff.phase !== "failed") {
      cancelRelease();
      cancelWake();
      heldToolCaption = null;
      return renderStrip(
        handoffPhaseText(chat.handoff, chat.active_agent.harness),
        `HANDOFF_${chat.handoff.phase}`,
        columnClass,
      );
    }
    const state = chat?.active_agent.activity_state ?? null;
    const label = labelForActivityState(
      state,
      events,
      getQueuedMessagesForChat(chatId).length > 0,
      chat?.active_agent.compaction_cause ?? null,
    );

    const now = Date.now();
    // How long the turn has run, for a row that reports one: from the transcript's record of the
    // message that opened it, so a reloaded page counts from the same moment.
    const turnStartedAt = runningTurnStartedAt(chatId, state, label !== null, events);
    const turnElapsed = elapsedSince(turnStartedAt, now);
    if (turnStartedAt !== null) scheduleTick(turnStartedAt, now);
    if (state === "TOOL_RUNNING" && label !== null) {
      // Active tool -> (re)start the hold window; cancel any pending release.
      cancelRelease();
      heldToolCaption = label;
      heldUntil = now + TOOL_CAPTION_MIN_MS;
    } else if (state === "THINKING" && heldToolCaption !== null && now < heldUntil) {
      // Still working, but the tool cleared fast -- keep the caption up briefly so
      // it doesn't flash, then release. Schedule a redraw at the release point.
      if (releaseTimer === null) {
        releaseTimer = window.setTimeout(() => {
          releaseTimer = null;
          heldToolCaption = null;
          m.redraw();
        }, heldUntil - now);
      }
      cancelWake();
      return renderStrip(heldToolCaption, "TOOL_RUNNING", columnClass, turnElapsed);
    } else {
      // IDLE / null (turn ended), window expired, or nothing held -> release now.
      cancelRelease();
      heldToolCaption = null;
    }

    if (label === null) {
      // Nothing else is going on, so this is the only moment the wake-up
      // caption may take the strip -- a real turn always outranks it.
      const wakeDeadline = wakeUpSpinnerDeadline(events, now);
      if (wakeDeadline !== null) {
        if (wakeTimer === null) {
          wakeTimer = window.setTimeout(() => {
            wakeTimer = null;
            m.redraw();
          }, wakeDeadline - now);
        }
        return renderStrip(WAKE_LABEL, WAKE_STATE, columnClass);
      }
      cancelWake();
      return sendStrip(chatId, columnClass, now) ?? startingStrip(attrs, columnClass, now);
    }
    cancelWake();
    return renderStrip(label, state, columnClass, turnElapsed);
  };

  return {
    onremove() {
      cancelRelease();
      cancelWake();
      cancelTick();
    },
    view(vnode) {
      const tasks = getChatById(vnode.attrs.chatId)?.active_agent.background_tasks ?? [];
      if (tasks.length === 0) return activityStrip(vnode.attrs, STRIP_COLUMN_CLASS);
      return m("div", { class: `agent-activity-stack ${STRIP_COLUMN_CLASS} flex flex-col gap-1` }, [
        m(BackgroundTasksLine, { tasks }),
        activityStrip(vnode.attrs, ""),
      ]);
    },
  };
}
