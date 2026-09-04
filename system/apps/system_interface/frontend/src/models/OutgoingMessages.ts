/**
 * Ephemeral, client-only "Sending…" bubbles -- the ONE optimistic state the
 * frontend is permitted to invent (contract A2). One is painted at the tail of the
 * transcript the instant the user POSTs a message, before the backend's own state
 * (its queued chip, or the committed transcript turn) catches up.
 *
 * Removal is BACKEND-DRIVEN and ORDERED, never timed: the backend reports the
 * message's real representation -- a live transcript ``user_message`` arriving, or a
 * new entry in the harness queue snapshot -- and ``noteBackendArrivals`` drops the
 * oldest bubble as each genuinely-new real item appears. The real representation is
 * already rendered when its id reaches here, so the real one shows FIRST and only
 * then is "Sending…" removed: the message is always visible in some form
 * ("reconciliation goes through the message", A2), never a gap. Correlation is
 * positional (oldest-first) with arrival-id dedup; there is no content matching, and
 * over-eager removal is harmless -- the real bubble is what shows.
 *
 * A send FAILURE is NOT rendered here: the caller drops the bubble (``dropOutgoing``)
 * and handles failure the original way -- a popup plus restoring the text to the
 * composer. A bubble that somehow never sees an arrival dies on reload; there is no
 * frontend timer, and it never gates real state.
 */
import m from "mithril";
import type { ChatId } from "../ids";

export interface OutgoingMessage {
  id: string;
  /** The text the user typed (shown verbatim), not the attachment-expanded form. */
  content: string;
}

const byAgent: Record<ChatId, OutgoingMessage[]> = {};
// Arrival ids already accounted for, per chat -- so a re-streamed transcript
// event or a re-pushed queued snapshot does not drop a bubble twice.
const seenArrivalIds: Record<ChatId, Set<string>> = {};
let nextId = 0;

/** Record a just-sent message as an optimistic "Sending…" bubble; returns its id
 *  so the caller can drop it on failure. */
export function addOutgoing(chatId: ChatId, content: string): string {
  const id = `outgoing-${nextId++}`;
  (byAgent[chatId] ??= []).push({ id, content });
  m.redraw();
  return id;
}

export function getOutgoingMessages(chatId: ChatId): OutgoingMessage[] {
  return byAgent[chatId] ?? [];
}

/** Remove a specific set of bubbles by id. Used by the interrupt path: it snapshots
 *  the agent's Sending bubble ids BEFORE the stop round-trip and clears exactly those
 *  once the interrupt succeeds. Passing the pre-interrupt snapshot (not "all bubbles for
 *  the agent") is deliberate -- a new message the user sends DURING the interrupt
 *  round-trip must keep its bubble, since it is not part of the returned block. */
export function clearOutgoing(chatId: ChatId, ids: readonly string[]): void {
  const list = byAgent[chatId];
  if (list === undefined || ids.length === 0) {
    return;
  }
  const toRemove = new Set(ids);
  const next = list.filter((entry) => !toRemove.has(entry.id));
  if (next.length !== list.length) {
    byAgent[chatId] = next;
    m.redraw();
  }
}

/** Remove a specific bubble -- used by the send-failure path (the message did not
 *  send; its text is returned to the composer by the caller). */
export function dropOutgoing(chatId: ChatId, id: string): void {
  const list = byAgent[chatId];
  if (list === undefined) {
    return;
  }
  const next = list.filter((entry) => entry.id !== id);
  if (next.length !== list.length) {
    byAgent[chatId] = next;
    m.redraw();
  }
}

function removeOldest(chatId: ChatId): void {
  const list = byAgent[chatId];
  if (list === undefined || list.length === 0) {
    return;
  }
  dropOutgoing(chatId, list[0].id);
}

/**
 * Backend-driven, ordered removal of "Sending…" bubbles (contract A2/A3b). Each
 * genuinely-new real user item for this agent -- a live transcript ``user_message``
 * (its event_id) or a new queued-snapshot entry (its queued_id) -- drops the oldest
 * bubble, so the optimistic bubble clears exactly as the real one appears. Because
 * the real representation is already rendered when its id arrives here, removal
 * always FOLLOWS arrival (real first, then remove) -- never a gap.
 *
 * Ids are deduped per agent, so a re-streamed event or a re-pushed snapshot does not
 * drop a bubble again. Correlation is positional (oldest-first) and never matches on
 * content. Over-eager removal is harmless: the real bubble is what shows, so at worst
 * the "Sending…" indicator clears a touch early -- never a duplicate.
 */
export function noteBackendArrivals(chatId: ChatId, ids: readonly string[]): void {
  if (ids.length === 0) {
    return;
  }
  const seen = (seenArrivalIds[chatId] ??= new Set());
  for (const id of ids) {
    if (seen.has(id)) {
      continue;
    }
    // Record every arrival id (so a re-stream/re-push cannot drop a later bubble),
    // and drop the oldest bubble -- a no-op when there are none.
    seen.add(id);
    removeOldest(chatId);
  }
}
