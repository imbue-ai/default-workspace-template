/**
 * The avatar's image (pinned-taskbar-entries plan section 6.2): an ``<img>`` whose ``src`` is the image
 * route with the workspace's design and the current mood, so a mood change is one attribute change and
 * the browser's image isolation keeps the SVG passive. A load that fails falls back to the default
 * design at the same mood.
 */

import m from "mithril";
import { avatarImageUrl } from "../model/api";
import type { AvatarMood, AvatarStatus } from "../model/records";
import type { AvatarState, TaskbarEntry } from "../reducers/desktopState";
import { appGlyph } from "./glyphs";

export interface AvatarImageAttrs {
  readonly design: string;
  readonly defaultDesign: string;
  readonly mood: AvatarMood;
  readonly class: string;
}

export const AvatarImage: m.Component<AvatarImageAttrs> = {
  view(vnode) {
    const { design, defaultDesign, mood } = vnode.attrs;
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
          class: imageClass,
        })
      : m.trust(appGlyph(entry.app, glyphSize)),
  };
}
