/**
 * A window's icon on the phone (the pill, the windows sheet's rows): the workspace's avatar for a pinned window
 * whose pin draws it, else the app's glyph.
 */

import m from "mithril";
import type { AppRecord, WindowRecord } from "../../model/records";
import type { AvatarState } from "../../reducers/desktopState";
import { AvatarImage } from "../AvatarImage";
import { appGlyph } from "../glyphs";

/** ``sizeClass`` sizes the box (and names it, where a caller needs to); ``glyphSize`` is the glyph's px. */
export function windowIcon(
  window: WindowRecord,
  app: AppRecord | undefined,
  avatar: AvatarState,
  sizeClass: string,
  glyphSize: number,
): m.Children {
  const isAvatar = window.is_pinned && app?.pin?.style === "avatar";
  return m(
    "span",
    {
      class:
        `${sizeClass} flex shrink-0 items-center justify-center overflow-hidden [&>svg]:size-full ` +
        (isAvatar ? "rounded-full bg-surface" : "rounded-(--desk-icon-radius)"),
    },
    isAvatar
      ? m(AvatarImage, {
          design: avatar.design,
          defaultDesign: avatar.defaultDesign,
          mood: avatar.status.mood,
          class: "size-full object-contain",
        })
      : m.trust(appGlyph(app, glyphSize)),
  );
}
