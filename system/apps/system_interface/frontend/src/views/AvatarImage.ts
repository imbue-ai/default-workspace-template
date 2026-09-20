/**
 * The avatar's image (pinned-taskbar-entries plan section 6.2): an ``<img>`` whose ``src`` is the image
 * route with the workspace's design and the current mood, so a mood change is one attribute change and
 * the browser's image isolation keeps the SVG passive. A load that fails falls back to the default
 * design at the same mood.
 */

import m from "mithril";
import { avatarImageUrl } from "../model/api";
import type { AvatarMood, AvatarStatus } from "../model/records";

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
export function avatarTooltip(title: string, status: AvatarStatus): string {
  return status.is_stale ? `${title} (status may be out of date)` : title;
}
