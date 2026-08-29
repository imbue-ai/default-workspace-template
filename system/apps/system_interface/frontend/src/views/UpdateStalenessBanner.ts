/**
 * Informational banner shown when the workspace's on-disk code has moved
 * under the running interface.
 *
 * The backend injects <meta name="system-interface-update-staleness"> into the
 * app shell (see update_staleness.py) with one of three variants: a failed
 * update's rollback could not restore a healthy workspace, an update apply was
 * interrupted mid-motion (its marker is still present), or the tree advanced
 * without this server restarting into it. All mean the page the user is
 * reading may not match the workspace's current code. The banner only informs
 * -- acting on it stays with the agent, so the copy points the user there
 * rather than offering an action surface. Dismissal is per page load: a reload
 * after the state resolves gets a shell with no tag at all.
 */

import m from "mithril";

const META_TAG_NAME = "system-interface-update-staleness";

export function getUpdateStalenessVariant(): string {
  const metaElement = document.querySelector(`meta[name="${META_TAG_NAME}"]`);
  return metaElement?.getAttribute("content") ?? "";
}

// A Map, not an object literal: the variant is whatever string the backend put
// in the tag, and an object lookup walks the prototype chain -- so a newer
// backend's `toString` or `constructor` would resolve to a truthy
// Object.prototype member and render it as the banner's text instead of
// degrading to no banner at all.
export const STALENESS_MESSAGES = new Map<string, string>([
  // The one variant that does not resolve on its own, so it is the one that
  // asks for something rather than telling the user to wait.
  [
    "update-emergency",
    "A workspace update failed and could not be undone cleanly, so parts of this workspace may be broken. " +
      "This one does not resolve on its own -- ask your agent to look at it.",
  ],
  // The marker this variant reads is present for the *whole* apply, not only
  // after an interruption, so the copy must be true of a healthy in-flight
  // update too -- it must not announce a rollback that may not be happening.
  [
    "update-interrupted",
    "A workspace update is part-way through, so you may be looking at the previous version. " +
      "It finishes or undoes itself automatically; if this notice is still here in a few minutes, ask your agent about it.",
  ],
  [
    "updated-not-activated",
    "Parts of this workspace were updated but not yet activated, so you may be looking at the previous version. " +
      "If this notice persists, ask your agent about it.",
  ],
]);

export function UpdateStalenessBanner(): m.Component {
  // Per-page-load dismissal only: the condition is transient by design, and a
  // fresh shell simply carries no tag once the workspace is consistent again.
  let hidden = false;
  return {
    view() {
      if (hidden) return null;
      const message = STALENESS_MESSAGES.get(getUpdateStalenessVariant());
      if (message === undefined) return null;
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
