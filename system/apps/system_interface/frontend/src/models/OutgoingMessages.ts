/**
 * Ephemeral, client-only "outgoing" messages -- the optimistic "Sending…" bubbles
 * shown at the very tail of the transcript the instant the user sends, before the
 * harness-sourced state (a queued bubble, or the committed turn) catches up.
 *
 * This is the ONE place the frontend paints optimistic state, and it only ever
 * shows "Sending…". Removal is ARRIVAL-DRIVEN, not timed: the backend's own
 * updates route through ``noteBackendArrivals`` (a live transcript ``user_message``
 * arriving, or a new queued-snapshot entry), and each genuinely-new arrival drops
 * the oldest bubble, so the optimistic bubble disappears exactly as the real one
 * appears -- no overlap. Correlation is positional (oldest-first) + arrival-id
 * dedup; there is NO content matching.
 *
 * A send FAILURE is NOT rendered here: the caller drops the bubble
 * (``dropOutgoing``) and handles failure the original way -- a popup plus
 * restoring the text to the composer. A delivered bubble that somehow never sees
 * an arrival is swept by an anti-strand fallback timer. It dies on reload; it
 * never gates real state.
 *
 * This module also owns the SHOULDER-TAP FREEZE (see the flush-freeze section
 * below): the same optimistic-overlay idea for the shoulder-tap action. While the
 * agent restarts and the queued messages are resent, the frozen (captured) group
 * is held greyed, and it is released on the very same backend-arrival signal that
 * clears a "Sending…" bubble -- so the frozen group disappears exactly as the
 * resent messages land, never leaving a stale hold or a gap.
 */
import m from "mithril";

import type { QueuedMessage } from "./AgentManager";

export interface OutgoingMessage {
  id: string;
  /** The text the user typed (shown verbatim), not the attachment-expanded form. */
  content: string;
}

// Anti-strand fallback: if a delivered send never produces an observable arrival,
// drop its bubble this long after the POST resolves. The fast path is
// arrival-driven, so this only fires in the unusual no-arrival case.
const FALLBACK_MS = 6000;

const byAgent: Record<string, OutgoingMessage[]> = {};
// Arrival ids already accounted for, per agent -- so a re-streamed transcript
// event or a re-pushed queued snapshot does not drop a bubble twice.
const seenArrivalIds: Record<string, Set<string>> = {};
// Anti-strand fallback timers, keyed by outgoing id.
const fallbackTimers: Record<string, ReturnType<typeof setTimeout>> = {};
let nextId = 0;

// In-flight send promises per agent. A send is not truly "done" until the backend
// confirms it (the message durably parked in the harness queue). Stop waits on these
// so a just-sent message parks in time to be drained back to the composer, and the
// shoulder tap waits so it folds into the flush -- rather than either racing the send
// and silently dropping it. Each promise drops itself from the set on settle.
const pendingSendsByAgent: Record<string, Set<Promise<unknown>>> = {};

// A stuck send must never hang Stop or the shoulder tap; after this cap the waiting
// action proceeds regardless. The backend confirms delivery before a send resolves, so
// this only bites a pathological hang, and the residual (a send that parks just after
// the action proceeds) is the same narrow capture window the backend already accepts.
const PENDING_SEND_WAIT_CAP_MS = 5000;

/** Register a just-started send so Stop / the shoulder tap can wait for it to park. The
 *  promise removes itself on settle (success or failure) and redraws so the greyed
 *  shoulder-tap button re-enables. */
export function registerPendingSend(agentId: string, promise: Promise<unknown>): void {
  const set = (pendingSendsByAgent[agentId] ??= new Set());
  set.add(promise);
  const forget = (): void => {
    set.delete(promise);
    m.redraw();
  };
  promise.then(forget, forget);
}

/** True while at least one send for this agent is still in flight (its message may not
 *  be parked yet). Used to grey out the shoulder tap so it cannot fire mid-send. */
export function hasPendingSends(agentId: string): boolean {
  const set = pendingSendsByAgent[agentId];
  return set !== undefined && set.size > 0;
}

/** Wait for the sends in flight for this agent AT CALL TIME to settle -- each parks its
 *  message durably before resolving -- bounded by a cap so a stuck send cannot hang the
 *  caller. Sends started after this call are not awaited. */
export async function awaitPendingSends(agentId: string): Promise<void> {
  const set = pendingSendsByAgent[agentId];
  if (set === undefined || set.size === 0) {
    return;
  }
  const settled = Promise.allSettled([...set]);
  const capped = new Promise<void>((resolve) => setTimeout(resolve, PENDING_SEND_WAIT_CAP_MS));
  await Promise.race([settled, capped]);
}

function clearFallback(id: string): void {
  const timer = fallbackTimers[id];
  if (timer !== undefined) {
    clearTimeout(timer);
    delete fallbackTimers[id];
  }
}

/** Record a just-sent message as an optimistic "Sending…" bubble; returns its id
 *  so the caller can resolve it (on success) or drop it (on failure). */
export function addOutgoing(agentId: string, content: string): string {
  const id = `outgoing-${nextId++}`;
  (byAgent[agentId] ??= []).push({ id, content });
  m.redraw();
  return id;
}

export function getOutgoingMessages(agentId: string): OutgoingMessage[] {
  return byAgent[agentId] ?? [];
}

/** Remove a specific bubble -- used by the send-failure path (the message did not
 *  send; its text is returned to the composer by the caller). */
export function dropOutgoing(agentId: string, id: string): void {
  const list = byAgent[agentId];
  if (list === undefined) {
    return;
  }
  const next = list.filter((entry) => entry.id !== id);
  if (next.length !== list.length) {
    byAgent[agentId] = next;
    clearFallback(id);
    m.redraw();
  }
}

/** The send POST resolved (delivery confirmed): arm the anti-strand fallback so
 *  the bubble cannot linger forever if no backend arrival is ever observed. The
 *  fast path is still ``noteBackendArrivals``. */
export function resolveOutgoing(agentId: string, id: string): void {
  clearFallback(id);
  fallbackTimers[id] = setTimeout(() => dropOutgoing(agentId, id), FALLBACK_MS);
}

function removeOldest(agentId: string): void {
  const list = byAgent[agentId];
  if (list === undefined || list.length === 0) {
    return;
  }
  dropOutgoing(agentId, list[0].id);
}

/**
 * Route backend arrivals through the optimistic layer: each genuinely-new real
 * user item for this agent -- a live transcript ``user_message`` (its event_id)
 * or a new queued-snapshot entry (its queued_id) -- drops the oldest "Sending…"
 * bubble, so the optimistic bubble clears exactly as the real one lands.
 *
 * Ids are deduped per agent, so a re-streamed event or a re-pushed snapshot does
 * not drop a bubble again. Removal is positional (oldest-first) and never matches
 * on content. Over-eager removal is harmless: the real bubble is what shows, so at
 * worst the "Sending…" indicator clears a touch early -- never a duplicate.
 */
export function noteBackendArrivals(agentId: string, ids: readonly string[]): void {
  if (ids.length === 0) {
    return;
  }
  const seen = (seenArrivalIds[agentId] ??= new Set());
  let sawNew = false;
  for (const id of ids) {
    if (seen.has(id)) {
      continue;
    }
    // Record every arrival id (so a re-stream/re-push cannot drop a later bubble),
    // and drop the oldest bubble -- a no-op when there are none.
    seen.add(id);
    sawNew = true;
    removeOldest(agentId);
  }
  // A genuinely-new arrival also releases a shoulder-tap freeze: the resent
  // messages are landing, so the greyed hold clears exactly as they appear.
  if (sawNew && flushFreezeByAgent[agentId] !== undefined) {
    releaseFlushFreeze(agentId);
  }
}

// --- Shoulder-tap freeze --------------------------------------------------
//
// While the shoulder-tap action restarts the agent and resends the queue, the
// backend snapshot momentarily empties (its harness queue is killed) before the
// resent messages reappear as committed turns. Rendering the live snapshot then
// would blip the messages out and back. Instead we capture them on click and hold
// them greyed until a backend arrival (routed through ``noteBackendArrivals``
// above, exactly like a "Sending…" bubble) signals the resent messages are
// landing -- with a cap as the only fallback. No visible countdown.
const FLUSH_FREEZE_CAP_MS = 20_000;

interface FlushFreeze {
  messages: QueuedMessage[];
}

const flushFreezeByAgent: Record<string, FlushFreeze> = {};
const flushFreezeCapTimers: Record<string, ReturnType<typeof setTimeout>> = {};

/** Begin holding the captured queued messages greyed while the flush restarts the
 *  agent. Released by the next backend arrival (see ``noteBackendArrivals``) or the
 *  cap, whichever comes first. */
export function startFlushFreeze(agentId: string, messages: QueuedMessage[]): void {
  flushFreezeByAgent[agentId] = { messages };
  const existing = flushFreezeCapTimers[agentId];
  if (existing !== undefined) {
    clearTimeout(existing);
  }
  flushFreezeCapTimers[agentId] = setTimeout(() => releaseFlushFreeze(agentId), FLUSH_FREEZE_CAP_MS);
  m.redraw();
}

/** The frozen (captured) messages to render greyed, or ``undefined`` when not frozen. */
export function getFlushFreeze(agentId: string): FlushFreeze | undefined {
  return flushFreezeByAgent[agentId];
}

/** Release the freeze so the real backend snapshot renders again -- called by an
 *  arrival, the cap, or the flush's own failure path. */
export function releaseFlushFreeze(agentId: string): void {
  const wasFrozen = flushFreezeByAgent[agentId] !== undefined;
  delete flushFreezeByAgent[agentId];
  const timer = flushFreezeCapTimers[agentId];
  if (timer !== undefined) {
    clearTimeout(timer);
    delete flushFreezeCapTimers[agentId];
  }
  if (wasFrozen) {
    m.redraw();
  }
}
