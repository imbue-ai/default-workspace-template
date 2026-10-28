/**
 * A participant's picture, as every surface of the chat draws it: their profile picture in a round frame, or a
 * monogram of their name on a neutral fill when they have none (an invited email, a visiting agent, the owner of an
 * unshared workspace).
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
  readonly extra?: string;
}

export function participantPicture(attrs: ParticipantPictureAttrs): m.Vnode {
  const style = `width: ${attrs.size}px; height: ${attrs.size}px; font-size: ${Math.round(attrs.size * 0.5)}px`;
  return m(
    "span",
    {
      class:
        "participant-picture flex shrink-0 select-none items-center justify-center overflow-hidden rounded-full " +
        "bg-fill-hover font-medium text-secondary " +
        (attrs.extra ?? ""),
      style,
      title: attrs.name,
      "data-participant-picture": "",
    },
    attrs.pictureUrl !== null
      ? m("img", { class: "h-full w-full object-cover", src: attrs.pictureUrl, alt: monogramOf(attrs.name) })
      : monogramOf(attrs.name),
  );
}
