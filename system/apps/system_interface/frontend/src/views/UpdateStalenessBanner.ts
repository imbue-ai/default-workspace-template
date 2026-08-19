/**
 * Informational banner shown when the workspace's on-disk code has moved
 * under the running interface.
 *
 * The backend injects <meta name="system-interface-update-staleness"> into the
 * app shell (see update_staleness.py) with one of two variants: an update
 * apply was interrupted mid-motion (its marker is still present), or the tree
 * advanced without this server restarting into it. Both mean the page the user
 * is reading may not match the workspace's current code. The banner only
 * informs -- acting on it stays with the agent, so the copy points the user
 * there rather than offering an action surface. Dismissal is per page load: a
 * reload after the state resolves gets a shell with no tag at all.
 */

import m from "mithril";

const META_TAG_NAME = "system-interface-update-staleness";

export function getUpdateStalenessVariant(): string {
  const metaElement = document.querySelector(`meta[name="${META_TAG_NAME}"]`);
  return metaElement?.getAttribute("content") ?? "";
}

export const STALENESS_MESSAGES: Record<string, string> = {
  "update-interrupted":
    "A workspace update was interrupted before it finished and is being rolled back automatically. " +
    "If this notice is still here in a few minutes, ask your agent about it.",
  "updated-not-activated":
    "Parts of this workspace were updated but not yet activated, so you may be looking at the previous version. " +
    "If this notice persists, ask your agent about it.",
};

export function UpdateStalenessBanner(): m.Component {
  // Per-page-load dismissal only: the condition is transient by design, and a
  // fresh shell simply carries no tag once the workspace is consistent again.
  let hidden = false;
  return {
    view() {
      if (hidden) return null;
      const message = STALENESS_MESSAGES[getUpdateStalenessVariant()];
      if (!message) return null;
      return m("div.update-staleness-banner", [
        m("span.update-staleness-banner-text", message),
        m(
          "button.update-staleness-banner-btn",
          {
            onclick: () => {
              hidden = true;
            },
          },
          "Dismiss",
        ),
      ]);
    },
  };
}
