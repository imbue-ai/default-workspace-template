/**
 * Ephemeral, client-only "outgoing" messages -- the optimistic "Sending…" bubbles
 * shown at the very tail of the transcript the instant the user sends, before the
 * harness-sourced state (a queued bubble, or the committed turn) catches up.
 *
 * This is the ONE place the frontend paints optimistic state, and it is
 * deliberately thin, self-terminating, and fully decoupled from the backend:
 *  - an entry is "sending" while its send POST is in flight,
 *  - it flips to "failed" (and stays, until the next send) if the POST rejects,
 *  - it is removed a short beat after the POST resolves -- the backend confirms
 *    delivery before resolving, so by then the real bubble (a queued-group entry
 *    or a committed turn) is arriving, and the beat just avoids a flash of nothing.
 *
 * No content matching, no correlation, no persistence -- it dies on reload. If it
 * is ever wrong it self-heals on the next send or reload; it never gates real state.
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

// How long after a send POST resolves to keep the bubble up, so the real bubble
// has a beat to render and we never flash an empty gap. Purely cosmetic; small.
const SETTLE_MS = 450;

const byAgent: Record<string, OutgoingMessage[]> = {};
let nextId = 0;

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
    m.redraw();
  }
}

/** The send POST resolved: the message reached the backend (delivery is confirmed
 *  before the POST resolves). Keep the bubble a short beat so the real bubble
 *  renders first, then drop it. */
export function resolveOutgoing(agentId: string, id: string): void {
  setTimeout(() => removeOutgoing(agentId, id), SETTLE_MS);
}

/** The send POST rejected: the message was NOT accepted. Flip to a persistent
 *  "failed" state so the user plainly sees it did not send. */
export function failOutgoing(agentId: string, id: string, error: string): void {
  const entry = byAgent[agentId]?.find((candidate) => candidate.id === id);
  if (entry !== undefined) {
    entry.status = "failed";
    entry.error = error;
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
