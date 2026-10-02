/**
 * The avatar's image (pinned-taskbar-entries plan section 6.2): an ``<img>`` whose ``src`` is the image
 * route with the workspace's design and the current mood, so a mood change is one attribute change and
 * the browser's image isolation keeps the SVG passive. A load that fails falls back to the default
 * design at the same mood.
 *
 * The workspace's own character is the one design not drawn this way: it is a rig running in this
 * page rather than a drawing, which is what lets it answer a press, so it renders as a component
 * and the ``<img>`` isolation does not apply. The isolation exists because a registered design is
 * markup a user or an agent authored; the character is this bundle's own code. Do not widen that
 * exception to anything that comes from the catalog.
 */

import m from "mithril";
import { IMBUE_CHARACTER_DESIGN_ID, ImbueCharacter } from "./character/ImbueCharacter";
import { avatarImageUrl } from "../model/api";
import type { AvatarMood, AvatarStatus } from "../model/records";
import type { AvatarState, TaskbarEntry } from "../reducers/desktopState";
import { appGlyph } from "./glyphs";

export interface AvatarImageAttrs {
  readonly design: string;
  readonly defaultDesign: string;
  readonly mood: AvatarMood;
  /** Whether the user is at this entry's window. Only the character answers it, and only an entry
   *  knows: a caller that is drawing the avatar as an identifying icon leaves it out. */
  readonly isAttending?: boolean;
  readonly class: string;
}

export const AvatarImage: m.Component<AvatarImageAttrs> = {
  view(vnode) {
    const { design, defaultDesign, mood } = vnode.attrs;
    if (design === IMBUE_CHARACTER_DESIGN_ID) {
      return m(ImbueCharacter, { mood, isAttending: vnode.attrs.isAttending === true, class: vnode.attrs.class });
    }
    return m("img", {
      "data-avatar-image": design,
      src: avatarImageUrl(design, mood),
      alt: "",
      draggable: false,
      class: vnode.attrs.class,
      onerror: (event: Event) => {
        const image = event.currentTarget as HTMLImageElement;
        const fallback = avatarImageUrl(defaultDesign, mood);
        if (design !== defaultDesign && !image.src.endsWith(fallback)) image.src = fallback;
      },
    });
  },
};

/**
 * Whether the user is at this entry's window: focused, and neither minimized nor pulled out into a
 * desktop window of the chrome's own.
 *
 * The last two are asked separately rather than assumed to clear focus: the shell can hold focus on
 * a window it is not drawing.
 */
function isUserAtWindow(entry: TaskbarEntry): boolean {
  return entry.isFocused && !entry.isMinimized && !entry.isDetached;
}

/** What an avatar entry's tooltip reads: the title, and a warning when the status may be out of date. */
function avatarTooltip(title: string, status: AvatarStatus): string {
  return status.is_stale ? `${title} (status may be out of date)` : title;
}

/** The parts of a pinned entry's rendering its style decides, shared by the bar entry and the floating one. */
export interface EntryStyleParts {
  readonly isAvatar: boolean;
  /** ``data-mood`` and ``data-stale`` in the avatar style; nothing in the plain one. */
  readonly attrs: { readonly "data-mood": AvatarMood | undefined; readonly "data-stale": string | undefined };
  /** The title, with the stale warning in the avatar style. */
  readonly tooltip: string;
  /** The avatar wearing the mood in the avatar style, else the app's icon at ``glyphSize``. */
  readonly image: m.Children;
}

export function entryStyleParts(
  entry: TaskbarEntry,
  avatar: AvatarState,
  glyphSize: number,
  imageClass: string,
): EntryStyleParts {
  const isAvatar = entry.look?.style === "avatar";
  return {
    isAvatar,
    attrs: {
      "data-mood": isAvatar ? avatar.status.mood : undefined,
      "data-stale": isAvatar ? (avatar.status.is_stale ? "true" : "false") : undefined,
    },
    tooltip: isAvatar ? avatarTooltip(entry.title, avatar.status) : entry.title,
    image: isAvatar
      ? m(AvatarImage, {
          design: avatar.design,
          defaultDesign: avatar.defaultDesign,
          mood: avatar.status.mood,
          isAttending: isUserAtWindow(entry),
          class: imageClass,
        })
      : m.trust(appGlyph(entry.app, glyphSize)),
  };
}
