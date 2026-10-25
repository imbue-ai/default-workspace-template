/**
 * The people in the tray (the multiplayer spec, section 6.2): one 32px picture per roster entry other than the
 * viewer and the workspace agent, in roster order, then the viewer's own. An entry that is not here is faded; an
 * invited email is a dashed circle; a visiting agent a monogram tile. An entry hops once when it becomes connected
 * and when a bubble of its appears.
 * With four or more entries the pictures stack, each tucked under the one before it with a ring of the bar's
 * colour, and the row spreads while the pointer is over it. A click on a person opens the Lounge with a mention of
 * them; a click on your own picture or an invited email does nothing. Hovering an entry opens its card (the caller's).
 */

import m from "mithril";
import type { RosterParticipant } from "../../model/records";
import { presenceStateOf } from "../../model/Roster";
import { TrayAvatar } from "./TrayAvatar";

export const PERSON_PICTURE_SIZE_PX = 32;
/** From this many entries on, the pictures stack (S9). */
export const STACKING_THRESHOLD = 4;

/** The entries the strip draws, in order: everyone else in roster order, then the viewer's own entry; never the
 *  workspace agent, which has an entry of its own. */
export function peopleOrder(roster: readonly RosterParticipant[], viewer: string | null): RosterParticipant[] {
  const people = roster.filter((entry) => entry.kind !== "workspace_agent");
  return [...people.filter((entry) => entry.key !== viewer), ...people.filter((entry) => entry.key === viewer)];
}

/** Whether the strip stacks its pictures: four or more entries. */
export function isStacked(count: number): boolean {
  return count >= STACKING_THRESHOLD;
}

/** The accessible name of one entry: the name, with "(you)" on your own and "(invited)" on an invited email. */
export function entryTitle(entry: RosterParticipant, isOwn: boolean): string {
  if (entry.kind === "invited") return `${entry.email ?? entry.display_name} (invited)`;
  return isOwn ? `${entry.display_name} (you)` : entry.display_name;
}

export interface PeopleStripAttrs {
  readonly roster: readonly RosterParticipant[];
  readonly viewer: string | null;
  readonly now: Date;
  /** The keys of the entries hopping now: they just arrived, or a bubble of theirs just appeared. */
  readonly hopping: ReadonlySet<string>;
  readonly onPersonClick: (entry: RosterParticipant) => void;
  readonly onHoverStart: (entry: RosterParticipant, element: HTMLElement) => void;
  readonly onHoverEnd: () => void;
}

export const PeopleStrip: m.Component<PeopleStripAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    const entries = peopleOrder(attrs.roster, attrs.viewer);
    if (entries.length === 0) return null;
    const stacked = isStacked(entries.length);
    return m(
      "div",
      {
        "data-tray-widget": "people",
        "data-stacked": stacked ? "true" : "false",
        class: "tray-people group/people flex items-center " + (stacked ? "hover:gap-1" : "gap-1"),
        "aria-label": "People in this workspace",
      },
      entries.map((entry, index) => {
        const isOwn = entry.key === attrs.viewer;
        const isFaded = presenceStateOf(entry, attrs.now) === "offline";
        const isClickable = !isOwn && entry.kind !== "invited";
        const isHopping = attrs.hopping.has(entry.key);
        return m(
          "button",
          {
            key: entry.key,
            type: "button",
            "data-tray-person": entry.key,
            "data-tray-self": isOwn ? "true" : null,
            "data-tray-hopping": isHopping ? "true" : null,
            class:
              "tray-person relative flex shrink-0 items-center justify-center rounded-full border-0 p-0 " +
              "outline-none focus-visible:ring-2 focus-visible:ring-accent " +
              // The bar's surface under a faded picture, so it never shows the one tucked under it.
              (isFaded ? "bg-(--desk-taskbar-surface) " : "bg-transparent ") +
              (isClickable ? "cursor-pointer " : "cursor-default ") +
              (isHopping ? "tray-hop " : "") +
              (stacked ? "ring-2 ring-(--desk-taskbar-surface) " : "") +
              (stacked && index > 0
                ? "-ml-(--desk-tray-stack-overlap) transition-[margin] group-hover/people:ml-0 "
                : ""),
            style: stacked ? { zIndex: String(entries.length - index) } : {},
            "aria-label": entryTitle(entry, isOwn),
            onclick: () => {
              if (isClickable) attrs.onPersonClick(entry);
            },
            onmouseenter: (event: MouseEvent) => attrs.onHoverStart(entry, event.currentTarget as HTMLElement),
            onmouseleave: attrs.onHoverEnd,
          },
          m(TrayAvatar, { entry, sizePx: PERSON_PICTURE_SIZE_PX, isFaded }),
        );
      }),
    );
  },
};
