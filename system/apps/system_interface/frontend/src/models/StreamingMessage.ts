/**
 * SSE connection management for real-time agent events.
 * Connects to the backend's SSE stream and appends new events.
 *
 * Streams are keyed by agentId so multiple chat panels can subscribe
 * independently; each agent gets its own EventSource.
 */

import { apiUrl } from "../base-path";
import { ReconnectBackoff } from "./backoff";
import { appendEvents, fetchEvents, type TranscriptEvent } from "./Response";
import { parseJsonMessage } from "./ws-json";
import { openLoginModal } from "./ClaudeAuth";

const activeStreams = new Map<string, EventSource>();
// Set so an error-triggered reconnect timeout can tell an intentional close
// from a transient error.
const explicitlyDisconnectedAgents = new Set<string>();
// Per-agent reconnect backoff, so a healthy stream's success does not reset an
// unhealthy stream's growing delay.
const backoffByAgent = new Map<string, ReconnectBackoff>();

function getBackoff(agentId: string): ReconnectBackoff {
  let backoff = backoffByAgent.get(agentId);
  if (backoff === undefined) {
    backoff = new ReconnectBackoff();
    backoffByAgent.set(agentId, backoff);
  }
  return backoff;
}
// Holds SSE deltas that arrive while a snapshot fetch is in flight (on either
// the initial mount or a reconnect), so fetchEvents replacing
// eventsByAgent[agentId] does not drop them.
const inFlightSnapshotBuffersByAgent = new Map<string, TranscriptEvent[]>();

// The server's idle keepalive, delivered as a named SSE event precisely so the
// watchdog below can see it (a `:` comment line keeps the socket warm but the
// EventSource API never surfaces one). It carries no payload.
const SSE_PING_EVENT = "ping";
/**
 * How long a stream may go without receiving ANY bytes -- no transcript event
 * and no keepalive ping -- before it is presumed dead and rebuilt.
 *
 * This is not "no chat activity for 30s". The server sends a ping every ~8 idle
 * seconds, so a healthy but silent conversation is producing traffic the whole
 * time and never trips this; it takes roughly three consecutive missed pings.
 *
 * What it does catch is a half-dead connection (tunnel drop, sleep/wake), which
 * produces no `error` and no `close`: the transcript just stops while the
 * terminal's WebSocket, which has its own keepalive, keeps looking healthy. That
 * is the "chat out of sync with the TUI" report, and silence is the only
 * evidence of it available to the client.
 */
export const SSE_SILENCE_TIMEOUT_MS = 30_000;
// Watchdog tick. Short relative to the timeout, so recovery lands within about
// SSE_SILENCE_TIMEOUT_MS plus one tick.
const SSE_WATCHDOG_INTERVAL_MS = 5_000;
// `EventSource.OPEN`. Written as the literal because the constant lives on the
// EventSource constructor, which tests substitute.
const EVENT_SOURCE_OPEN = 1;

const lastStreamActivityMsByAgent = new Map<string, number>();
// One timer for all streams; started with the first connection and stopped once
// the last one goes away, so an idle app holds no interval.
let watchdogTimer: ReturnType<typeof setInterval> | null = null;

function noteStreamActivity(agentId: string): void {
  lastStreamActivityMsByAgent.set(agentId, Date.now());
}

function startWatchdog(): void {
  if (watchdogTimer !== null) {
    return;
  }
  watchdogTimer = setInterval(checkStreamLiveness, SSE_WATCHDOG_INTERVAL_MS);
}

function stopWatchdogIfNoStreams(): void {
  if (watchdogTimer !== null && activeStreams.size === 0) {
    clearInterval(watchdogTimer);
    watchdogTimer = null;
  }
}

/** Tear down and rebuild any stream that has gone silent past the timeout.
 *  Recovery goes through the snapshot path so the transcript is re-read whole,
 *  covering whatever was missed while the connection was quietly dead. */
function checkStreamLiveness(): void {
  const now = Date.now();
  for (const [agentId, eventSource] of [...activeStreams]) {
    const silentForMs = now - (lastStreamActivityMsByAgent.get(agentId) ?? now);
    if (silentForMs < SSE_SILENCE_TIMEOUT_MS) {
      continue;
    }
    // Only step in when the browser still believes the connection is up. A
    // CONNECTING socket is one the browser noticed dying and is already
    // retrying -- and our own onerror path owns that case -- so intervening
    // there would just race it with a second reconnect.
    if (eventSource.readyState !== EVENT_SOURCE_OPEN) {
      continue;
    }
    console.warn(`SSE stream for agent ${agentId} silent for ${Math.round(silentForMs / 1000)}s; reconnecting`);
    eventSource.close();
    activeStreams.delete(agentId);
    lastStreamActivityMsByAgent.delete(agentId);
    void reconnectWithSnapshot(agentId);
  }
  stopWatchdogIfNoStreams();
}

// Claude auth is mind-global, so an auth-error on any agent's stream
// opens the single shared login modal (see models/ClaudeAuth.ts) -- no
// per-agent routing needed.
function openLoginModalIfAuthError(event: TranscriptEvent): void {
  if (event.type === "assistant_message" && event.is_auth_error === true) {
    openLoginModal();
  }
}

export interface StreamingMessage {
  conversationId: string;
  userPrompt: string;
  model: string | null;
  assistantContent: string;
  finalized: boolean;
  error: string | null;
}

export function connectToStream(agentId: string): void {
  if (activeStreams.has(agentId)) {
    return;
  }

  // A fresh connect supersedes any prior explicit-disconnect tombstone.
  explicitlyDisconnectedAgents.delete(agentId);

  const eventSource = new EventSource(apiUrl(`/api/agents/${encodeURIComponent(agentId)}/stream`));
  activeStreams.set(agentId, eventSource);
  noteStreamActivity(agentId);
  startWatchdog();

  eventSource.onopen = () => {
    // A successful (re)connection resets this agent's backoff.
    getBackoff(agentId).reset();
    noteStreamActivity(agentId);
  };

  // Server keepalive: proves the connection is alive on a stream with nothing
  // to say. Deliberately does nothing else -- it is not a transcript event.
  eventSource.addEventListener(SSE_PING_EVENT, () => {
    noteStreamActivity(agentId);
  });

  eventSource.onmessage = (messageEvent: MessageEvent) => {
    noteStreamActivity(agentId);
    const raw = parseJsonMessage<{ type?: string }>(messageEvent.data);
    if (raw === null) {
      return;
    }
    const event = raw as TranscriptEvent;
    const pending = inFlightSnapshotBuffersByAgent.get(agentId);
    if (pending !== undefined) {
      pending.push(event);
    } else {
      appendEvents(agentId, [event]);
    }
    openLoginModalIfAuthError(event);
  };

  eventSource.onerror = () => {
    if (activeStreams.get(agentId) === eventSource) {
      eventSource.close();
      activeStreams.delete(agentId);
      lastStreamActivityMsByAgent.delete(agentId);
      stopWatchdogIfNoStreams();
      setTimeout(() => {
        const wasExplicitlyDisconnected = explicitlyDisconnectedAgents.delete(agentId);
        if (!wasExplicitlyDisconnected && !activeStreams.has(agentId)) {
          void reconnectWithSnapshot(agentId);
        }
      }, getBackoff(agentId).nextDelay());
    }
  };
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
export async function loadSnapshotWithStream(agentId: string): Promise<void> {
  // Subscribe to SSE before the snapshot fetch so deltas that arrive
  // between the snapshot read and the EventSource being registered land in
  // `buffer` instead of being dropped. Hold `buffer` by reference (not via
  // map lookup in `finally`) so a concurrent load that replaces the
  // map slot cannot orphan our buffered events.
  const buffer: TranscriptEvent[] = [];
  inFlightSnapshotBuffersByAgent.set(agentId, buffer);
  connectToStream(agentId);
  try {
    await fetchEvents(agentId);
  } finally {
    if (inFlightSnapshotBuffersByAgent.get(agentId) === buffer) {
      inFlightSnapshotBuffersByAgent.delete(agentId);
    }
    if (buffer.length > 0 && !explicitlyDisconnectedAgents.has(agentId)) {
      appendEvents(agentId, buffer);
    }
  }
}

async function reconnectWithSnapshot(agentId: string): Promise<void> {
  try {
    await loadSnapshotWithStream(agentId);
  } catch (error) {
    console.warn(`Snapshot refetch failed for agent ${agentId} during SSE reconnect`, error);
  }
}

export function disconnectFromStream(agentId: string): void {
  // Always record the intent, even with no active stream, so a pending
  // error-triggered reconnect timeout sees the tombstone and stays down.
  explicitlyDisconnectedAgents.add(agentId);
  // Drop the backoff so a later fresh connectToStream starts from the base
  // delay rather than inheriting a stale grown delay.
  backoffByAgent.delete(agentId);
  lastStreamActivityMsByAgent.delete(agentId);
  const eventSource = activeStreams.get(agentId);
  if (eventSource !== undefined) {
    eventSource.close();
    activeStreams.delete(agentId);
  }
  stopWatchdogIfNoStreams();
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
