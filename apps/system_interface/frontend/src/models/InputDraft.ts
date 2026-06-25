/**
 * One-shot channel for handing text from a transcript choice card to the
 * MessageInput composer, keyed by agentId.
 *
 * A choice card rendered inside the transcript (see views/choice-cards.ts) lives
 * in a different part of the component tree from the composer, so it can't reach
 * the input directly. Instead it stages the choice's text here; the MessageInput
 * for that agent consumes it on the next redraw. The composer then *submits* the
 * text as a message (so a click feels reactive), or -- when the text is empty
 * ("I have something in mind") -- just focuses the box for the user to type. The
 * submit/focus decision lives in the composer; this store only carries the text.
 *
 * Mirrors the module-level store pattern used by PendingMessages.ts (plain map +
 * an explicit redraw) rather than introducing any new state mechanism.
 */

import m from "mithril";

// Pending prefill text per agent. Empty string is a meaningful value -- it means
// "clear the box and focus it" (e.g. the "I have something in mind" card) -- so
// presence is tracked by Map membership, not by truthiness.
const pendingDraftByAgentId = new Map<string, string>();

/** Queue `text` to be dropped into `agentId`'s composer on the next redraw. */
export function setInputDraft(agentId: string, text: string): void {
  pendingDraftByAgentId.set(agentId, text);
  // Nudge a redraw so the composer picks the draft up even when this is called
  // from a non-DOM context; from a DOM event handler mithril would redraw anyway.
  m.redraw();
}

/**
 * Take and clear the pending draft for `agentId`. Returns the queued string
 * (possibly empty) when one was pending, or null when there was nothing queued.
 * The empty-vs-null distinction is load-bearing: an empty draft still focuses
 * and clears the box, whereas null leaves the composer untouched.
 */
export function consumeInputDraft(agentId: string): string | null {
  if (!pendingDraftByAgentId.has(agentId)) {
    return null;
  }
  const text = pendingDraftByAgentId.get(agentId) ?? "";
  pendingDraftByAgentId.delete(agentId);
  return text;
}
