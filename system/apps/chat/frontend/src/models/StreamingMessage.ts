/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by chatId so multiple chat panels can subscribe
 * independently; each chat gets its own EventSource.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import {
  appendEvents,
  fetchEvents,
  getEventCount,
  getFirstOffset,
  getLastEventId,
  hasMoreAfter,
  isTranscriptLoaded,
  requestEventsAfter,
  type TranscriptEvent,
} from "./Response";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";
import { addChatActivityListener, addChatsUpdatedListener } from "./Chats";
import { applyReplyDraftFrame, clearReplyDraft, isReplyDraftFrame } from "./ReplyDrafts";

const activeStreams = new Map<string, EventSource>();
// Set so an error-triggered reconnect timeout can tell an intentional close
// from a transient error.
const explicitlyDisconnectedChats = new Set<string>();
// Per-chat reconnect backoff, so a healthy stream's success does not reset an
// unhealthy stream's growing delay.
const backoffByChat = new Map<string, ReconnectBackoff>();

function getBackoff(chatId: string): ReconnectBackoff {
  let backoff = backoffByChat.get(chatId);
  if (backoff === undefined) {
    backoff = new ReconnectBackoff();
    backoffByChat.set(chatId, backoff);
  }
  return backoff;
}
// Holds SSE deltas that arrive while a snapshot fetch is in flight (on either
// the initial mount or a reconnect), so fetchEvents resetting the chat's held
// window (TranscriptStore.reset) to the snapshot does not drop them.
const inFlightSnapshotBuffersByChat = new Map<string, TranscriptEvent[]>();
// Pending reconnect timers, ONE per chat. Both failure paths (a stream error
// and a failed snapshot refetch) schedule through scheduleReconnectWithSnapshot,
// which no-ops while a timer is already pending. Without this dedup each failed
// cycle would spawn two future loops (the new stream's error handler plus the
// snapshot retry), multiplying attempts for as long as the backend stays down.
const pendingReconnectTimersByChat = new Map<string, ReturnType<typeof setTimeout>>();

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(chatId: string): void {
  if (activeStreams.has(chatId)) {
    return;
  }

  // A fresh connect supersedes any prior explicit-disconnect tombstone.
  explicitlyDisconnectedChats.delete(chatId);

  console.info(`[si-sse] opening stream for chat ${chatId}`);
  const eventSource = new EventSource(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/stream`));
  activeStreams.set(chatId, eventSource);

  eventSource.onopen = () => {
    console.info(`[si-sse] stream open for chat ${chatId}`);
    // A successful (re)connection resets this agent's backoff.
    getBackoff(chatId).reset();
  };

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
    if (raw === null) {
      return;
    }
    // A draft is not a transcript event: it is held beside the transcript, never in it.
    if (isReplyDraftFrame(raw)) {
      applyReplyDraftFrame(chatId, raw);
      return;
    }
    const event = raw as TranscriptEvent;
    const pending = inFlightSnapshotBuffersByChat.get(chatId);
    if (pending !== undefined) {
      pending.push(event);
    } else {
      appendEvents(chatId, [event]);
    }
  };

  eventSource.onerror = () => {
    if (activeStreams.get(chatId) === eventSource) {
      eventSource.close();
      activeStreams.delete(chatId);
      clearReplyDraft(chatId);
      console.warn(`[si-sse] stream error for chat ${chatId}`);
      scheduleReconnectWithSnapshot(chatId);
    }
  };
}

/**
 * Schedule one reconnect-with-snapshot attempt after this agent's current
 * backoff delay. No-op while an attempt is already pending, so the stream's
 * error handler and a failed snapshot refetch cannot stack parallel retry
 * loops. The pending timer consumes an explicit-disconnect tombstone the same
 * way the old error path did: a disconnect issued during the delay keeps the
 * stream down.
 */
function scheduleReconnectWithSnapshot(chatId: string): void {
  if (pendingReconnectTimersByChat.has(chatId)) {
    return;
  }
  const delayMs = getBackoff(chatId).nextDelay();
  console.info(`[si-sse] scheduling reconnect for chat ${chatId} in ${delayMs}ms`);
  pendingReconnectTimersByChat.set(
    chatId,
    setTimeout(() => {
      pendingReconnectTimersByChat.delete(chatId);
      const wasExplicitlyDisconnected = explicitlyDisconnectedChats.delete(chatId);
      if (!wasExplicitlyDisconnected) {
        void reconnectWithSnapshot(chatId);
      }
    }, delayMs),
  );
}

/**
 * Open the live SSE stream and fetch the snapshot together, buffering any SSE
 * deltas that arrive while the snapshot fetch is in flight.
 *
 * `fetchEvents` resets the chat's held window (`TranscriptStore.reset`) to the snapshot,
 * so a delta that arrives between the stream opening and the snapshot landing
 * would otherwise be overwritten and lost. Both the initial mount and the
 * reconnect path go through here so neither can drop events. Re-throws fetch
 * errors so the caller can surface a load error; buffered deltas are flushed
 * first regardless.
 */
export async function loadSnapshotWithStream(chatId: string): Promise<void> {
  // Subscribe to SSE before the snapshot fetch so deltas that arrive
  // between the snapshot read and the EventSource being registered land in
  // `buffer` instead of being dropped. Hold `buffer` by reference (not via
  // map lookup in `finally`) so a concurrent load that replaces the
  // map slot cannot orphan our buffered events.
  const buffer: TranscriptEvent[] = [];
  inFlightSnapshotBuffersByChat.set(chatId, buffer);
  connectToStream(chatId);
  try {
    await fetchEvents(chatId);
  } finally {
    if (inFlightSnapshotBuffersByChat.get(chatId) === buffer) {
      inFlightSnapshotBuffersByChat.delete(chatId);
    }
    if (buffer.length > 0 && !explicitlyDisconnectedChats.has(chatId)) {
      appendEvents(chatId, buffer);
    }
  }
}

async function reconnectWithSnapshot(chatId: string): Promise<void> {
  try {
    await loadSnapshotWithStream(chatId);
    console.info(`[si-sse] snapshot loaded for chat ${chatId}`);
  } catch (error) {
    // Until the snapshot lands, the stream (if it connected) is appending
    // deltas onto the pre-outage window, so events emitted during the outage
    // are missing from it. A single failure must not be terminal -- that
    // permanently desynchronizes the transcript from the server -- so keep
    // retrying until the snapshot succeeds or the panel disconnects.
    console.warn(`[si-sse] snapshot refetch failed for chat ${chatId}`, error);
    scheduleReconnectWithSnapshot(chatId);
  }
}

export function disconnectFromStream(chatId: string): void {
  console.info(`[si-sse] explicit disconnect for chat ${chatId}`);
  // Always record the intent, even with no active stream, so a pending
  // error-triggered reconnect timeout sees the tombstone and stays down.
  explicitlyDisconnectedChats.add(chatId);
  const pendingTimer = pendingReconnectTimersByChat.get(chatId);
  if (pendingTimer !== undefined) {
    clearTimeout(pendingTimer);
    pendingReconnectTimersByChat.delete(chatId);
  }
  // Drop the backoff so a later fresh connectToStream starts from the base
  // delay rather than inheriting a stale grown delay.
  backoffByChat.delete(chatId);
  const eventSource = activeStreams.get(chatId);
  if (eventSource !== undefined) {
    eventSource.close();
    activeStreams.delete(chatId);
  }
  clearReplyDraft(chatId);
}

// The most events one catch-up request fetches; a longer run is fetched by the next one.
const CATCH_UP_LIMIT = 200;

/**
 * Fetch what the live stream should have delivered after the last event the page holds.
 *
 * The stream is a low-latency hint and the transcript route the source of truth, and nothing
 * else notices an event the stream never carried (one written between the snapshot and the
 * stream registering, or one a stalled connection swallowed): it stays missing until the
 * page reloads. So whenever a turn ends, and whenever the page is shown again, the page asks
 * the route what follows its last event. Missing events at the end are appended in place;
 * one missing inside the window (the route places the window's end further along than the
 * page does) reloads the transcript with the stream, as a reconnect does.
 */
export async function catchUpLiveTail(chatId: string): Promise<void> {
  const lastEventId = getLastEventId(chatId);
  if (lastEventId === null || !isTranscriptLoaded(chatId) || hasMoreAfter(chatId)) {
    return;
  }
  const heldEnd = getFirstOffset(chatId) + getEventCount(chatId);
  let result;
  try {
    result = await requestEventsAfter(chatId, lastEventId, CATCH_UP_LIMIT);
  } catch (error) {
    console.warn(`[si-sse] catch-up fetch failed for chat ${chatId}`, error);
    return;
  }
  // Where the route places the first event after the held last one, and how long it says the
  // transcript is. Either beyond the window means an event the stream never carried sits
  // inside the window, where appending cannot put it.
  const offset = result.offset ?? heldEnd;
  const total = result.total ?? heldEnd + result.events.length;
  const isMissingInside = result.events.length === 0 ? total > heldEnd : offset > heldEnd;
  if (isMissingInside) {
    console.warn(`[si-sse] chat ${chatId} is missing events the stream did not deliver; reloading it`);
    await loadSnapshotWithStream(chatId).catch((error: unknown) => {
      console.warn(`[si-sse] reload after a missed event failed for chat ${chatId}`, error);
    });
    return;
  }
  // A window the stream moved while the fetch was out is checked again on the next trigger.
  if (result.events.length === 0 || getLastEventId(chatId) !== lastEventId) {
    return;
  }
  console.warn(`[si-sse] caught up ${result.events.length} event(s) the stream did not deliver for chat ${chatId}`);
  appendEvents(chatId, result.events);
  if (result.events.length === CATCH_UP_LIMIT) {
    await catchUpLiveTail(chatId);
  }
}

// The last chat status each streamed chat was seen with, to see a turn end by its status.
const lastStatusByChat = new Map<string, string>();

/**
 * Catch up every streamed chat whose turn just ended, and every streamed chat when the page is shown again.
 *
 * A turn's end is seen two ways, since either can be all the page gets: the activity reaching
 * IDLE (as soon as the reply lands), and the chat's status leaving `working` (once the turn's
 * hooks finish, or when the chat reads as stopped without its activity ever having moved).
 */
function installCatchUpTriggers(): void {
  addChatActivityListener((chatId, previous, current) => {
    if (current === "IDLE" && previous !== null && previous !== "IDLE" && activeStreams.has(chatId)) {
      void catchUpLiveTail(chatId);
    }
  });
  addChatsUpdatedListener((chats) => {
    const previousByChat = new Map(lastStatusByChat);
    // Rebuilt from each push, so a chat that left the list leaves the map with it.
    lastStatusByChat.clear();
    for (const chat of chats) {
      lastStatusByChat.set(chat.chat_id, chat.status);
      const previous = previousByChat.get(chat.chat_id);
      if (previous === "working" && chat.status !== "working" && activeStreams.has(chat.chat_id)) {
        void catchUpLiveTail(chat.chat_id);
      }
    }
  });
  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        for (const chatId of activeStreams.keys()) {
          void catchUpLiveTail(chatId);
        }
      }
    });
  }
}

installCatchUpTriggers();

// Compatibility shims
export function getStreamingMessage(_chatId: string): StreamingMessage | null {
  return null;
}

export function isStreaming(): boolean {
  return false;
}

export function clearStreamingMessage(): void {}

export function consumeLastFinalizedMessage(): StreamingMessage | null {
  return null;
}

export function startStreamingMessage(): void {}
export function appendStreamingDelta(): void {}
export function finalizeStreamingMessage(): void {}
export function markStreamingError(): void {}
