/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by ChatId so multiple chat panels can subscribe
 * independently; each chat gets its own EventSource.
 */

import { apiUrl } from "../base-path";
import type { ChatId } from "../ids";
import { ReconnectBackoff } from "./backoff";
import { appendEvents, fetchEvents, type TranscriptEvent } from "./Response";
import { parseJsonMessage } from "./ws-json";

const activeStreams = new Map<ChatId, EventSource>();
// Set so an error-triggered reconnect timeout can tell an intentional close
// from a transient error.
const explicitlyDisconnectedAgents = new Set<ChatId>();
// Per-agent reconnect backoff, so a healthy stream's success does not reset an
// unhealthy stream's growing delay.
const backoffByAgent = new Map<ChatId, ReconnectBackoff>();

function getBackoff(chatId: ChatId): ReconnectBackoff {
  let backoff = backoffByAgent.get(chatId);
  if (backoff === undefined) {
    backoff = new ReconnectBackoff();
    backoffByAgent.set(chatId, backoff);
  }
  return backoff;
}
// Holds SSE deltas that arrive while a snapshot fetch is in flight (on either
// the initial mount or a reconnect), so fetchEvents replacing
// eventsByAgent[agentId] does not drop them.
const inFlightSnapshotBuffersByAgent = new Map<ChatId, TranscriptEvent[]>();
// Pending reconnect timers, ONE per agent. Both failure paths (a stream error
// and a failed snapshot refetch) schedule through scheduleReconnectWithSnapshot,
// which no-ops while a timer is already pending. Without this dedup each failed
// cycle would spawn two future loops (the new stream's error handler plus the
// snapshot retry), multiplying attempts for as long as the backend stays down.
const pendingReconnectTimersByAgent = new Map<ChatId, ReturnType<typeof setTimeout>>();

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(chatId: ChatId): void {
  if (activeStreams.has(chatId)) {
    return;
  }

  // A fresh connect supersedes any prior explicit-disconnect tombstone.
  explicitlyDisconnectedAgents.delete(chatId);

  console.info(`[si-sse] opening stream for chat ${chatId}`);
  const eventSource = new EventSource(apiUrl(`/api/chats/${encodeURIComponent(chatId)}/stream`));
  activeStreams.set(chatId, eventSource);

  eventSource.onopen = () => {
    console.info(`[si-sse] stream open for chat ${chatId}`);
    // A successful (re)connection resets this chat's backoff.
    getBackoff(chatId).reset();
  };

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
    if (raw === null) {
      return;
    }
    const event = raw as TranscriptEvent;
    const pending = inFlightSnapshotBuffersByAgent.get(chatId);
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
function scheduleReconnectWithSnapshot(chatId: ChatId): void {
  if (pendingReconnectTimersByAgent.has(chatId)) {
    return;
  }
  const delayMs = getBackoff(chatId).nextDelay();
  console.info(`[si-sse] scheduling reconnect for chat ${chatId} in ${delayMs}ms`);
  pendingReconnectTimersByAgent.set(
    chatId,
    setTimeout(() => {
      pendingReconnectTimersByAgent.delete(chatId);
      const wasExplicitlyDisconnected = explicitlyDisconnectedAgents.delete(chatId);
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
 * `fetchEvents` replaces `eventsByAgent[agentId]` wholesale with the snapshot,
 * so a delta that arrives between the stream opening and the snapshot landing
 * would otherwise be overwritten and lost. Both the initial mount and the
 * reconnect path go through here so neither can drop events. Re-throws fetch
 * errors so the caller can surface a load error; buffered deltas are flushed
 * first regardless.
 */
export async function loadSnapshotWithStream(chatId: ChatId): Promise<void> {
  // Subscribe to SSE before the snapshot fetch so deltas that arrive
  // between the snapshot read and the EventSource being registered land in
  // `buffer` instead of being dropped. Hold `buffer` by reference (not via
  // map lookup in `finally`) so a concurrent load that replaces the
  // map slot cannot orphan our buffered events.
  const buffer: TranscriptEvent[] = [];
  inFlightSnapshotBuffersByAgent.set(chatId, buffer);
  connectToStream(chatId);
  try {
    await fetchEvents(chatId);
  } finally {
    if (inFlightSnapshotBuffersByAgent.get(chatId) === buffer) {
      inFlightSnapshotBuffersByAgent.delete(chatId);
    }
    if (buffer.length > 0 && !explicitlyDisconnectedAgents.has(chatId)) {
      appendEvents(chatId, buffer);
    }
  }
}

async function reconnectWithSnapshot(chatId: ChatId): Promise<void> {
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

export function disconnectFromStream(chatId: ChatId): void {
  console.info(`[si-sse] explicit disconnect for chat ${chatId}`);
  // Always record the intent, even with no active stream, so a pending
  // error-triggered reconnect timeout sees the tombstone and stays down.
  explicitlyDisconnectedAgents.add(chatId);
  const pendingTimer = pendingReconnectTimersByAgent.get(chatId);
  if (pendingTimer !== undefined) {
    clearTimeout(pendingTimer);
    pendingReconnectTimersByAgent.delete(chatId);
  }
  // Drop the backoff so a later fresh connectToStream starts from the base
  // delay rather than inheriting a stale grown delay.
  backoffByAgent.delete(chatId);
  const eventSource = activeStreams.get(chatId);
  if (eventSource !== undefined) {
    eventSource.close();
    activeStreams.delete(chatId);
  }
}

// Compatibility shims
export function getStreamingMessage(_agentId: string): StreamingMessage | null {
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
