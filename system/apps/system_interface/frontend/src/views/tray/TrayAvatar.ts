/**
 * One picture in the tray (the multiplayer spec, section 6.2): a person's profile picture, a monogram on their
 * colour when there is none, an invited email's dashed circle with an envelope, or a visiting agent's monogram
 * tile. Drawn at the size the caller gives, faded while the entry is not here.
 */

import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { RosterParticipant } from "../../model/records";
import { monogramOf } from "../../model/Roster";

/** The opacity of an entry that is not here: faint, over the bar's surface so a faded face never shows the one
 *  behind it. */
export const DISCONNECTED_OPACITY = 0.35;

export interface TrayAvatarAttrs {
  readonly entry: RosterParticipant;
  readonly sizePx: number;
  readonly isFaded: boolean;
  readonly class?: string;
}

const ENVELOPE_GLYPH_SIZE = 14;
const BASE_CLASS = "tray-avatar flex shrink-0 items-center justify-center overflow-hidden select-none ";

export const TrayAvatar: m.Component<TrayAvatarAttrs> = {
  view(vnode) {
    const { entry, sizePx, isFaded } = vnode.attrs;
    const extra = vnode.attrs.class ?? "";
    const style: Record<string, string> = { width: `${sizePx}px`, height: `${sizePx}px` };
    if (isFaded) style.opacity = String(DISCONNECTED_OPACITY);
    if (entry.kind === "invited") {
      return m(
        "span",
        {
          class: `${BASE_CLASS}rounded-full border border-dashed border-strong bg-surface text-faint ${extra}`,
          style,
          "data-tray-avatar": "invited",
        },
        m.trust(icon("inbox", { size: ENVELOPE_GLYPH_SIZE })),
      );
    }
    if (entry.kind === "visiting_agent") {
      return m(
        "span",
        {
          class: `${BASE_CLASS}rounded-[32%] bg-surface-secondary text-(length:--font-size-helper) font-semibold text-secondary ${extra}`,
          style,
          "data-tray-avatar": "visiting-agent",
        },
        monogramOf(entry),
      );
    }
    const hasPicture = entry.profile_picture_url !== null;
    if (!hasPicture) {
      style.backgroundColor = entry.color;
      style.color = "#ffffff";
    }
    return m(
      "span",
      {
        class: `${BASE_CLASS}rounded-full bg-surface text-(length:--font-size-helper) font-semibold text-secondary ${extra}`,
        style,
        "data-tray-avatar": entry.kind === "workspace_agent" ? "workspace-agent" : "person",
      },
      hasPicture
        ? m("img", {
            class: "h-full w-full object-cover",
            src: entry.profile_picture_url,
            alt: monogramOf(entry),
            draggable: false,
          })
        : monogramOf(entry),
    );
  },
};
