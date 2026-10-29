/**
 * A participant's picture, as every surface of the chat draws it: their profile picture in a round frame, or a
 * monogram of their name when they have none (an invited email, a visiting agent, the owner of an unshared
 * workspace) or when the picture's URL would not load (a profile URL can 404 or expire). The monogram sits on the
 * participant's roster colour, as the desktop's tray draws it, so a person without a picture looks the same on
 * every surface; with no colour known it sits on a neutral fill.
 */

import m from "mithril";

/** The first letter of a name, uppercased; "?" for an empty one. */
export function monogramOf(name: string): string {
  const first = name.trim().slice(0, 1);
  return first ? first.toUpperCase() : "?";
}

export interface ParticipantPictureAttrs {
  readonly name: string;
  readonly pictureUrl: string | null;
  /** The frame's side, in CSS px. */
  readonly size: number;
  /** The participant's roster colour, the monogram's fill; null or absent for the neutral fill. */
  readonly color?: string | null;
  readonly extra?: string;
}

const ParticipantPicture: m.ClosureComponent<ParticipantPictureAttrs> = () => {
  // The picture URL whose load failed: the monogram stands in for it, until a different URL is tried afresh.
  let failedUrl: string | null = null;
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      // An empty URL is no picture, and a URL that failed to load falls back to the monogram too.
      const pictureUrl = attrs.pictureUrl || null;
      const hasPicture = pictureUrl !== null && pictureUrl !== failedUrl;
      const color = attrs.color ?? null;
      const isColored = !hasPicture && color !== null;
      const style =
        `width: ${attrs.size}px; height: ${attrs.size}px; font-size: ${Math.round(attrs.size * 0.5)}px` +
        (isColored ? `; background-color: ${color}` : "");
      return m(
        "span",
        {
          class:
            "participant-picture flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full " +
            (isColored ? "font-semibold text-on-accent " : "bg-fill-hover font-medium text-secondary ") +
            (attrs.extra ?? ""),
          style,
          title: attrs.name,
          "data-participant-picture": "",
        },
        hasPicture
          ? m("img", {
              class: "h-full w-full object-cover",
              src: pictureUrl,
              alt: monogramOf(attrs.name),
              onerror: () => {
                failedUrl = pictureUrl;
              },
            })
          : monogramOf(attrs.name),
      );
    },
  };
};

export function participantPicture(attrs: ParticipantPictureAttrs): m.Vnode<ParticipantPictureAttrs> {
  return m(ParticipantPicture, attrs);
}
