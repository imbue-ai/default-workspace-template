/**
 * The Onboarding checklist page: its title, and the items in order, one row each, with a check mark on the done
 * ones. A row whose pointer can be shown here is a button, and a click shows the pointer whether or not the item
 * is done; a row whose pointer cannot be shown is listed plain. The page is its own scroller, since the shared
 * base styles pin the body to the viewport.
 * Markers the tests use: ``data-checklist-title``, ``data-checklist-item`` (the item's id), ``data-done``.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { ChecklistItem } from "../models/ChecklistItems";
import { isPointerAvailable } from "../models/pointers";

export const PAGE_TITLE = "Onboarding checklist";

const CHECK_GLYPH_SIZE = 14;
const ROW_CLASS = "flex w-full items-center gap-3 rounded-md px-2 py-2 text-left type-body text-primary";

export interface ChecklistPageAttrs {
  /** The items in order; null until they have been read. */
  readonly items: readonly ChecklistItem[] | null;
  /** Whether a chrome stands above the page that draws coach marks on its own controls. */
  readonly showsCoachMarks: boolean;
  /** Show the pointer of the item with this id. */
  readonly onPick: (itemId: string) => void;
}

/** The mark at the head of a row: a filled circle holding a check for a done item, an empty circle otherwise. */
function checkMark(isDone: boolean): m.Vnode {
  return m(
    "span",
    {
      "aria-hidden": "true",
      class:
        "flex size-5 shrink-0 items-center justify-center rounded-full border " +
        (isDone ? "border-accent bg-accent text-on-accent" : "border-strong"),
    },
    isDone ? m.trust(icon("check", { size: CHECK_GLYPH_SIZE })) : null,
  );
}

function itemRow(item: ChecklistItem, attrs: ChecklistPageAttrs): m.Vnode {
  const content = [checkMark(item.isDone), m("span", { class: "min-w-0" }, item.label)];
  const marks = { "data-checklist-item": item.id, "data-done": item.isDone ? "true" : "false" };
  if (!isPointerAvailable(item.id, attrs.showsCoachMarks)) {
    return m("li", { key: item.id }, m("div", { ...marks, class: ROW_CLASS }, content));
  }
  return m(
    "li",
    { key: item.id },
    m(
      "button",
      {
        ...marks,
        type: "button",
        class: `${ROW_CLASS} cursor-pointer hover:bg-fill-hover`,
        onclick: () => attrs.onPick(item.id),
      },
      content,
    ),
  );
}

export const ChecklistPage: m.Component<ChecklistPageAttrs> = {
  view(vnode) {
    const { items } = vnode.attrs;
    return m("div", { class: "onboarding-checklist-page h-screen w-full overflow-y-auto bg-page px-4 py-5" }, [
      m("h1", { "data-checklist-title": "", class: "m-0 px-2 type-heading text-primary" }, PAGE_TITLE),
      items === null
        ? null
        : m(
            "ul",
            { class: "m-0 mt-3 flex list-none flex-col gap-1 p-0" },
            items.map((item) => itemRow(item, vnode.attrs)),
          ),
    ]);
  },
};
