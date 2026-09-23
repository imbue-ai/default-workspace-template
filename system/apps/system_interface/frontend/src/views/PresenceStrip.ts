/**
 * The Presence tray widget: who is here, and who you are. One avatar per connected user,
 * plus (on a share) the link that re-runs your own sign-in after your account record changed (a new
 * verified email, say); names and avatars are the connector's profile and need no refresh.
 *
 * Renders nothing while nobody is recorded, which is every unshared workspace: an owner
 * without an account is never recorded, so a workspace that carries no identity keeps the
 * same chrome it always had.
 */

import m from "mithril";
import { getOwnIdentity, getPresentUsers, identityRefreshUrl } from "../model/Presence";
import type { PresentUser } from "../model/records";

const AVATAR_CLASS =
  "flex h-6 w-6 shrink-0 items-center justify-center overflow-hidden rounded-full border border-default bg-surface text-(length:--font-size-row) text-secondary";

/** The letter an avatar-less user wears: the first letter of their name, else of their email. */
export function presenceInitial(user: Pick<PresentUser, "display_name" | "email">): string {
  const source = user.display_name?.trim() || user.email;
  return source.slice(0, 1).toUpperCase();
}

/** The hover text for one user: the name with the email beside it, so a self-chosen name never stands alone. */
export function presenceTitle(user: Pick<PresentUser, "display_name" | "email" | "owner">): string {
  const who = user.display_name ? `${user.display_name} (${user.email})` : user.email;
  return user.owner ? `${who} - owner` : who;
}

function avatar(user: PresentUser): m.Children {
  return m(
    "span",
    { class: AVATAR_CLASS, title: presenceTitle(user), "data-presence-user": user.user_id },
    user.avatar_url
      ? m("img", { class: "h-full w-full object-cover", src: user.avatar_url, alt: presenceInitial(user) })
      : presenceInitial(user),
  );
}

export function PresenceStrip(): m.Component {
  return {
    view() {
      const users = getPresentUsers();
      if (users.length === 0) return null;
      const refreshUrl = getOwnIdentity() === null ? null : identityRefreshUrl();
      return m(
        "div",
        {
          "data-tray-widget": "presence",
          class: "presence-strip flex items-center gap-1 px-1",
          "aria-label": "Connected users",
        },
        [
          users.map((user) => avatar(user)),
          refreshUrl === null
            ? null
            : m(
                "a",
                {
                  class: "presence-refresh ml-1 type-helper text-secondary hover:text-primary",
                  href: refreshUrl,
                  title:
                    "Sign in again to pick up a change to your account (your name and avatar refresh on their own)",
                },
                "Refresh my sign-in",
              ),
        ],
      );
    },
  };
}
