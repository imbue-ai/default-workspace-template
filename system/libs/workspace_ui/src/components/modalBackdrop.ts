/**
 * Backdrop dismissal for every modal, defined once.
 *
 * Dismissal keys off mouse DOWN on the backdrop itself, not click. A click
 * fires wherever the press ENDED, so selecting text inside a dialog (name
 * fields especially) and releasing past its edge would read as a backdrop
 * click and throw the dialog -- and the half-typed edit -- away. A press that
 * STARTS on the backdrop is the only gesture that means "close this".
 *
 * The target check keeps presses inside the dialog from dismissing: the
 * handler sits on the backdrop element, so a press on any child of the dialog
 * arrives with a target other than the backdrop itself. The button check keeps
 * secondary presses from dismissing: a right-click reaches for a context menu,
 * not for "close this", and mousedown (unlike click) fires for every button.
 *
 * The backdrop scrolls a dialog taller than itself, and a press on its own
 * scrollbar arrives with the backdrop as its target too. That press scrolls;
 * it does not dismiss.
 */

/** Whether ``event`` landed on a scrollbar of ``backdrop``: past its client box, on an axis where a scrollbar takes
 *  room in it. */
function isOnScrollbar(event: MouseEvent, backdrop: HTMLElement): boolean {
  return (
    (backdrop.offsetWidth > backdrop.clientWidth && event.offsetX >= backdrop.clientWidth) ||
    (backdrop.offsetHeight > backdrop.clientHeight && event.offsetY >= backdrop.clientHeight)
  );
}

export function backdropDismissAttrs(onDismiss: () => void): {
  onmousedown: (event: MouseEvent) => void;
} {
  return {
    onmousedown(event: MouseEvent) {
      if (event.button !== 0 || event.target !== event.currentTarget) return;
      if (isOnScrollbar(event, event.currentTarget as HTMLElement)) return;
      onDismiss();
    },
  };
}
