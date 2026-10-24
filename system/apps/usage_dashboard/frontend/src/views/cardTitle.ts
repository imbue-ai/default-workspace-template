/** The one title every card on the page wears: the provider cards, the tiles, the chart, the breakdown and the turns. */

import m from "mithril";

export function cardTitle(text: string, tag: "h2" | "h3" | "span" = "h3"): m.Vnode {
  return m(tag, { class: "type-tag uppercase tracking-wide text-faint" }, text);
}
