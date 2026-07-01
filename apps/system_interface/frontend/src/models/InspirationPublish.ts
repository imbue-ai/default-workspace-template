/**
 * Global publish-proposal state for the in-UI inspiration-publish modal.
 *
 * Publishing an inspiration is a mind-global action (there is at most one
 * proposal awaiting user action at a time), so a single module-level
 * `currentProposal` drives one shared `InspirationPublishModal` rendered once
 * in `App.ts`, rather than every panel tracking its own copy.
 *
 * `openInspirationModal` is called from the AgentManager
 * `inspiration_publish_requested` dispatch case; it replaces any open proposal
 * (supersede), and `App.ts` keys the modal by slug so a superseding proposal
 * with a different slug remounts the component and re-prefills its form.
 * `closeInspirationModal` is slug-guarded: it is called both from the
 * `inspiration_publish_aborted` dispatch and from the modal's own
 * dismiss/confirm handlers, and only closes when the given slug matches the
 * shown proposal (a stale abort for a superseded slug is ignored).
 */

import m from "mithril";

// Untrusted proposal payload as broadcast by the backend
// `inspiration_publish_requested` event. thumbnail_svg is UNSANITIZED here;
// the view sanitizes with DOMPurify before rendering.
export interface InspirationProposal {
  slug: string;
  title: string;
  description: string;
  repo_name: string;
  visibility: string; // "private" | "public"
  thumbnail_svg: string;
}

let currentProposal: InspirationProposal | null = null;

export function getInspirationProposal(): InspirationProposal | null {
  return currentProposal;
}

// Slug accessor -- App.ts uses this as the mithril `key` so a superseding
// proposal (different slug) forces a fresh component instance that re-prefills.
export function getInspirationProposalSlug(): string | null {
  return currentProposal?.slug ?? null;
}

// Called from the AgentManager `inspiration_publish_requested` dispatch case.
// Replaces any open proposal (supersede); a new slug re-keys the modal.
export function openInspirationModal(proposal: InspirationProposal): void {
  currentProposal = proposal;
  m.redraw();
}

// Slug-guarded close, called from `inspiration_publish_aborted` dispatch AND
// from the modal's own dismiss/confirm handlers. Only closes if the slug
// matches the shown proposal (a stale abort for a superseded slug is ignored).
export function closeInspirationModal(slug: string | null): void {
  if (currentProposal === null) return;
  if (slug !== null && slug !== currentProposal.slug) return;
  currentProposal = null;
  m.redraw();
}
