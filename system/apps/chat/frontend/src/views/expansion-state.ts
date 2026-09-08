/**
 * In-memory record of which expandable transcript blocks (tool-call blocks,
 * system chips, progress-step bodies, markdown-wrapped tool output) the user
 * has expanded, keyed by a stable identity.
 *
 * Expansion used to live only in the DOM (a toggled class) or in per-component
 * state, so any vnode recreation -- the virtualized window sliding a row out
 * and back in, a streaming re-render, a section re-key -- silently collapsed
 * what the user had opened. Losing an expansion also snaps the row's height
 * down by the expanded content's size, which reads as the transcript jumping.
 * Session-scoped and in-memory on purpose: expansion is a reading aid, not
 * durable state.
 */

const expandedBlockKeys = new Set<string>();

export function isBlockExpanded(key: string): boolean {
  return expandedBlockKeys.has(key);
}

export function setBlockExpanded(key: string, isExpanded: boolean): void {
  if (isExpanded) {
    expandedBlockKeys.add(key);
  } else {
    expandedBlockKeys.delete(key);
  }
}

/** Flip and return the new state. */
export function toggleBlockExpanded(key: string): boolean {
  const next = !expandedBlockKeys.has(key);
  setBlockExpanded(key, next);
  return next;
}
