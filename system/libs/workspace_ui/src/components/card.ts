/* Card: the brand's surface for one thing among others -- a tile that opens something, a request in a
 * transcript, a panel that floats over a page. 16px corners and the default edge on the raised surface; an
 * interactive card deepens its edge and lifts under the pointer; a chosen one wears the focus colour round its
 * edge, as an open field does. A floating card (a popover, a tray card, a panel pinned over the page) is the overlay
 * surface with the floating shadow. A class builder: a card holds whatever its page puts in it. The Tailwind
 * scanner reads utility names from the literals in this file: keep every utility name a contiguous literal. */

export interface CardOptions {
  /** The whole card is one press: the pointer, a deeper edge and the raised shadow under it. */
  interactive?: boolean;
  /** The chosen one of a set of cards. */
  selected?: boolean;
  /** It floats over the page rather than sitting in it. */
  floating?: boolean;
  extra?: string;
}

export function cardClass(options: CardOptions = {}): string {
  const { interactive = false, selected = false, floating = false, extra = "" } = options;
  const parts = [
    "card rounded-2xl border",
    floating ? "bg-overlay-surface shadow-overlay" : "bg-body-surface-raised",
    selected ? "border-focus ring-1 ring-focus" : "border-default",
  ];
  if (interactive) {
    parts.push(
      "cursor-pointer transition-[border-color,box-shadow] duration-(--dur-base) hover:border-default-hover hover:shadow-raised",
    );
  }
  if (extra !== "") parts.push(extra);
  return parts.join(" ");
}
