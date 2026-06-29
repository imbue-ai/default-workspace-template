/**
 * Pure decision for whether the chat panel's scroll-management work (paging,
 * tail-following re-pins, viewport measurement) may run against its scroll
 * element right now.
 *
 * Dockview is configured with ``defaultRenderer: "always"``, so an inactive
 * chat tab is not destroyed: its content stays mounted while dockview hides an
 * ancestor with ``display: none``. The mithril component therefore keeps
 * redrawing (``m.redraw()`` is global) while hidden, but its scrollable element
 * then reports ``scrollTop``/``scrollHeight``/``clientHeight`` all as ``0``.
 * Running scroll logic against that zero-sized element corrupts the retained
 * scroll state -- it jumps a scrolled-up reader to the start of the conversation
 * and clobbers a tail-follower's saved position to 0. Skipping scroll work while
 * the element is not measurable preserves the position across a hide/show.
 *
 * It keys off whether the element currently has a usable layout box, so it is
 * DOM-free and unit-testable; the component reads the two values off the live
 * element and feeds them in.
 */

export interface ScrollElementMetrics {
  /** ``element.clientHeight`` -- 0 while the panel is hidden. */
  clientHeight: number;
  /**
   * ``element.offsetParent !== null``. ``offsetParent`` is ``null`` when the
   * element or an ancestor is ``display: none`` (as dockview sets on an inactive
   * tab), so this is ``false`` exactly when the panel is not rendered.
   */
  hasOffsetParent: boolean;
}

/**
 * Returns whether scroll-management work may run against the element. Both
 * signals must hold: a positive client height and a live offset parent. Either
 * one alone going false means the panel is hidden / has no usable size, so
 * scroll work must be skipped to avoid corrupting the retained position.
 */
export function isScrollMeasurable(metrics: ScrollElementMetrics): boolean {
  return metrics.clientHeight > 0 && metrics.hasOffsetParent;
}
