/**
 * The Presence tray widget: who is here. One profile picture per connected user, everyone else
 * first and the viewer's own entry last, ringed, with " (you)" on its hover text; names and
 * pictures are the connector's profile.
 *
 * Renders nothing until two or more users are connected: an owner alone, or the owner of an
 * unshared workspace (who is never recorded), keeps the same chrome they always had.
 */

import m from "mithril";
import { getOwnIdentity, getPresentUsers } from "../model/Presence";
import type { PresentUser } from "../model/records";

const PICTURE_CLASS =
  "flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-full border border-default bg-surface text-(length:--font-size-row) text-secondary";
const OWN_PICTURE_CLASS = " ring-2 ring-accent";

/** How many users have to be connected before the strip is drawn: one is just you. */
export const MIN_USERS_TO_DRAW = 2;

/** The letter a user without a picture wears: the first letter of their name, else of their email. */
export function presenceInitial(user: Pick<PresentUser, "display_name" | "email">): string {
  const source = user.display_name?.trim() || user.email;
  return source.slice(0, 1).toUpperCase();
}

/** The hover text for one user: the name with the email beside it, so a self-chosen name never stands alone. */
export function presenceTitle(user: Pick<PresentUser, "display_name" | "email" | "owner">): string {
  const who = user.display_name ? `${user.display_name} (${user.email})` : user.email;
  return user.owner ? `${who} - owner` : who;
}

/** The connected users as the strip orders them: everyone else in the shell's order, then the viewer's own entry. */
export function orderedForStrip(users: readonly PresentUser[], ownUserId: string | null): PresentUser[] {
  const others = users.filter((user) => user.user_id !== ownUserId);
  const own = users.filter((user) => user.user_id === ownUserId);
  return [...others, ...own];
}

function picture(user: PresentUser, isOwn: boolean): m.Children {
  return m(
    "span",
    {
      class: PICTURE_CLASS + (isOwn ? OWN_PICTURE_CLASS : ""),
      title: isOwn ? `${presenceTitle(user)} (you)` : presenceTitle(user),
      "data-presence-user": user.user_id,
      "data-presence-self": isOwn ? "true" : null,
    },
    user.profile_picture_url
      ? m("img", { class: "h-full w-full object-cover", src: user.profile_picture_url, alt: presenceInitial(user) })
      : presenceInitial(user),
  );
}

export function PresenceStrip(): m.Component {
  return {
    view() {
      const users = getPresentUsers();
      if (users.length < MIN_USERS_TO_DRAW) return null;
      const ownUserId = getOwnIdentity()?.user_id ?? null;
      return m(
        "div",
        {
          "data-tray-widget": "presence",
          class: "presence-strip flex items-center gap-1 px-1",
          "aria-label": "Connected users",
        },
        orderedForStrip(users, ownUserId).map((user) => picture(user, user.user_id === ownUserId)),
      );
    },
  };
}
