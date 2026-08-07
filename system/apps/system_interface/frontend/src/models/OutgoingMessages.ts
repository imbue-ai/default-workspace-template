/**
 * Ephemeral, client-only "outgoing" messages -- the optimistic "Sending…" bubbles
 * shown at the very tail of the transcript the instant the user sends, before the
 * harness-sourced state (a queued bubble, or the committed turn) catches up.
 *
 * This is the ONE place the frontend paints optimistic state. Removal is
 * ARRIVAL-DRIVEN, not timed: the backend's own updates route through
 * ``noteBackendArrivals`` (a live transcript ``user_message`` arriving, or a new
 * queued-snapshot entry), and each genuinely-new arrival drops the oldest
 * "Sending…" bubble. So the optimistic bubble disappears exactly as the real one
 * appears -- no overlap where both share the screen. Correlation is positional
 * (oldest-first) + arrival-id dedup; there is NO content matching.
 *
 * Terminal states: a POST rejection flips a bubble to a persistent "Failed to
 * send" (until the next send); a delivered bubble that somehow never sees an
 * arrival is swept by an anti-strand fallback timer. It dies on reload; it never
 * gates real state.
 */
import m from "mithril";

export type OutgoingStatus = "sending" | "failed";

export interface OutgoingMessage {
  id: string;
  /** The text the user typed (shown verbatim), not the attachment-expanded form. */
  content: string;
  status: OutgoingStatus;
  /** Present only when status === "failed". */
  error?: string;
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

function clearFallback(id: string): void {
  const timer = fallbackTimers[id];
  if (timer !== undefined) {
    clearTimeout(timer);
    delete fallbackTimers[id];
  }
}

/** Record a just-sent message as an optimistic "Sending…" bubble; returns its id
 *  so the caller can resolve or fail it when the send POST settles. */
export function addOutgoing(agentId: string, content: string): string {
  const id = `outgoing-${nextId++}`;
  (byAgent[agentId] ??= []).push({ id, content, status: "sending" });
  m.redraw();
  return id;
}

export function getOutgoingMessages(agentId: string): OutgoingMessage[] {
  return byAgent[agentId] ?? [];
}

function removeOutgoing(agentId: string, id: string): void {
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
  fallbackTimers[id] = setTimeout(() => {
    const entry = byAgent[agentId]?.find((candidate) => candidate.id === id);
    if (entry !== undefined && entry.status === "sending") {
      removeOutgoing(agentId, id);
    }
  }, FALLBACK_MS);
}

/** The send POST rejected: the message was NOT accepted. Flip to a persistent
 *  "failed" state (cancelling any fallback) so the user plainly sees it. */
export function failOutgoing(agentId: string, id: string, error: string): void {
  const entry = byAgent[agentId]?.find((candidate) => candidate.id === id);
  if (entry !== undefined) {
    entry.status = "failed";
    entry.error = error;
    clearFallback(id);
    m.redraw();
  }
}

/** Drop any failed entries for an agent -- called when the user sends again, so a
 *  fresh attempt clears the stale failure. */
export function clearFailedOutgoing(agentId: string): void {
  const list = byAgent[agentId];
  if (list === undefined) {
    return;
  }
  const next = list.filter((entry) => entry.status !== "failed");
  if (next.length !== list.length) {
    byAgent[agentId] = next;
    m.redraw();
  }
}

function removeOldestSending(agentId: string): void {
  const list = byAgent[agentId];
  if (list === undefined) {
    return;
  }
  const oldest = list.find((entry) => entry.status === "sending");
  if (oldest !== undefined) {
    removeOutgoing(agentId, oldest.id);
  }
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
  for (const id of ids) {
    if (seen.has(id)) {
      continue;
    }
    // Record every arrival id (so a re-stream/re-push cannot drop a later bubble),
    // and drop the oldest "Sending…" bubble -- a no-op when there are none.
    seen.add(id);
    removeOldestSending(agentId);
  }
}
