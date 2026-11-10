/**
 * The viewing chrome's chip (the multiplayer spec, section 8.3): while this window views a desktop that is not the
 * viewer's own, the tray is replaced by a filled chip in the owner's colour with their picture, "<Name> ·
 * <Desktop name>", and a "Stop viewing" button. The outline around the whole desktop is the App's; the chip runs
 * into it at the bar's corner.
 */

import m from "mithril";
import type { Desktop, RosterParticipant } from "../../model/records";
import { TrayAvatar } from "./TrayAvatar";

const OWNER_PICTURE_SIZE_PX = 28;
const STOP_VIEWING_LABEL = "Stop viewing";
const STOP_VIEWING_RETRY_LABEL = "Try again";
const STOP_VIEWING_FAILED_MESSAGE = "Couldn't leave this desktop";

export interface ViewingChipAttrs {
  readonly desktop: Desktop;
  /** The desktop's owner as the roster lists them; null for one whose owner has left the roster. */
  readonly owner: RosterParticipant | null;
  /** The owner's colour, which the outline wears too. */
  readonly color: string;
  readonly onStopViewing: () => void;
  /** How the last "Stop viewing" found nowhere to go, or null: the chip says so, and its button tries again only
   *  when that can find somewhere; when it cannot, the chip offers no button at all. */
  readonly stopFailure: { readonly canRetry: boolean } | null;
}

/** The name the chip gives a desktop's owner: the roster's, or nobody's for a desktop left behind. */
function viewedOwnerName(owner: RosterParticipant | null): string {
  return owner === null ? "Left behind" : owner.display_name;
}

export const ViewingChip: m.Component<ViewingChipAttrs> = {
  view(vnode) {
    const { desktop, owner, color, onStopViewing, stopFailure } = vnode.attrs;
    return m(
      "div",
      {
        "data-tray-widget": "viewing",
        "data-viewing-desktop": desktop.id,
        class:
          "tray-viewing -mr-2 flex items-center gap-2 self-stretch rounded-tl-xl pr-(--desk-viewing-outline) " +
          "pb-(--desk-viewing-outline) pl-3 text-on-accent",
        style: { backgroundColor: color },
      },
      [
        owner === null
          ? null
          : m(
              "span",
              { class: "flex shrink-0 items-center rounded-full ring-2 ring-white" },
              m(TrayAvatar, { entry: owner, sizePx: OWNER_PICTURE_SIZE_PX, isFaded: false }),
            ),
        m("span", { class: "flex min-w-0 items-baseline gap-1 text-(length:--font-size-row) font-medium" }, [
          m("span", { class: "truncate", "data-viewing-owner": "" }, viewedOwnerName(owner)),
          m("span", { class: "truncate font-normal opacity-75" }, ` · ${desktop.name}`),
        ]),
        stopFailure !== null
          ? m(
              "span",
              { class: "shrink-0 text-(length:--font-size-row) opacity-90", "data-stop-viewing-failed": "" },
              STOP_VIEWING_FAILED_MESSAGE,
            )
          : null,
        stopFailure !== null && !stopFailure.canRetry
          ? null
          : m(
              "button",
              {
                type: "button",
                "data-stop-viewing": "",
                class:
                  "shrink-0 cursor-pointer rounded-md border-0 bg-white/20 px-2 py-1 text-(length:--font-size-row) " +
                  "font-medium text-on-accent hover:bg-white/30 focus-visible:outline-2 focus-visible:outline-white",
                onclick: onStopViewing,
              },
              stopFailure === null ? STOP_VIEWING_LABEL : STOP_VIEWING_RETRY_LABEL,
            ),
      ],
    );
  },
};
