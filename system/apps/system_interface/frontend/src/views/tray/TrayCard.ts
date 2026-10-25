/**
 * The cards that hang above the tray's entries (the multiplayer spec, sections 6.3 and 6.4): one opens after
 * `CARD_HOVER_DELAY_MS` over an entry and stays while the pointer is on the entry or the card. This is the hover
 * bookkeeping; the person and agent cards are the content.
 */

import m from "mithril";

/** How long the pointer rests on an entry before its card opens. */
export const CARD_HOVER_DELAY_MS = 180;
/** How long the pointer may be between the entry and the card before the card goes. */
export const CARD_LEAVE_GRACE_MS = 120;

export const CARD_WIDTH_PX = 216;

/** Which card is open: a person's (by roster key) or the agent's. */
export type OpenCard = { readonly kind: "person"; readonly key: string } | { readonly kind: "agent" };

export interface TrayCardHover {
  /** The card open now, or null. */
  readonly current: () => OpenCard | null;
  /** Where it hangs: the entry's box, for the card to align its right edge to. */
  readonly anchor: () => DOMRect | null;
  /** The pointer came onto an entry: open its card after the delay (at once when a card is already open). */
  readonly enterEntry: (card: OpenCard, element: HTMLElement) => void;
  /** The pointer left an entry: the card goes after a grace, unless the pointer reaches it. */
  readonly leaveEntry: () => void;
  /** The pointer is on the card: it stays. */
  readonly enterCard: () => void;
  /** The pointer left the card. */
  readonly leaveCard: () => void;
  /** Put the card away now (a click, a key). */
  readonly close: () => void;
  /** Forget every timer. */
  readonly dispose: () => void;
}

export function createTrayCardHover(redraw: () => void): TrayCardHover {
  let open: OpenCard | null = null;
  let anchorElement: HTMLElement | null = null;
  let openTimer: ReturnType<typeof setTimeout> | null = null;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;

  function clearTimers(): void {
    if (openTimer !== null) clearTimeout(openTimer);
    if (closeTimer !== null) clearTimeout(closeTimer);
    openTimer = null;
    closeTimer = null;
  }

  function show(card: OpenCard, element: HTMLElement): void {
    open = card;
    anchorElement = element;
    redraw();
  }

  return {
    current: () => open,
    anchor: () => anchorElement?.getBoundingClientRect() ?? null,
    enterEntry: (card, element) => {
      clearTimers();
      if (open !== null) {
        show(card, element);
        return;
      }
      openTimer = setTimeout(() => {
        openTimer = null;
        show(card, element);
      }, CARD_HOVER_DELAY_MS);
    },
    leaveEntry: () => {
      if (openTimer !== null) clearTimeout(openTimer);
      openTimer = null;
      if (open === null) return;
      if (closeTimer !== null) clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        closeTimer = null;
        open = null;
        anchorElement = null;
        redraw();
      }, CARD_LEAVE_GRACE_MS);
    },
    enterCard: () => {
      if (closeTimer !== null) clearTimeout(closeTimer);
      closeTimer = null;
    },
    leaveCard: () => {
      if (closeTimer !== null) clearTimeout(closeTimer);
      closeTimer = setTimeout(() => {
        closeTimer = null;
        open = null;
        anchorElement = null;
        redraw();
      }, CARD_LEAVE_GRACE_MS);
    },
    close: () => {
      clearTimers();
      open = null;
      anchorElement = null;
      redraw();
    },
    dispose: clearTimers,
  };
}

export interface TrayCardFrameAttrs {
  /** The entry's box the card hangs above, right edges aligned. */
  readonly anchor: DOMRect | null;
  readonly label: string;
  readonly onEnter: () => void;
  readonly onLeave: () => void;
}

/** The card's box: 216px wide, hanging above the tray with its right edge on the entry's. */
export const TrayCardFrame: m.Component<TrayCardFrameAttrs> = {
  view(vnode) {
    const { anchor } = vnode.attrs;
    const right = anchor === null ? 0 : Math.max(0, window.innerWidth - anchor.right);
    const bottom = anchor === null ? 0 : Math.max(0, window.innerHeight - anchor.top + 6);
    return m(
      "div",
      {
        "data-tray-card": "",
        role: "dialog",
        "aria-label": vnode.attrs.label,
        class:
          "tray-card fixed z-(--z-popover) flex flex-col gap-2 rounded-xl border border-default bg-surface p-3 " +
          "text-(length:--font-size-row) text-primary shadow-overlay",
        style: { width: `${CARD_WIDTH_PX}px`, right: `${right}px`, bottom: `${bottom}px` },
        onmouseenter: vnode.attrs.onEnter,
        onmouseleave: vnode.attrs.onLeave,
      },
      vnode.children,
    );
  },
};
