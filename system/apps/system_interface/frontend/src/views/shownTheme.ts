/**
 * The page wears the theme of the desktop on screen (Desktop settings), or the one Desktop settings previews. A theme
 * can redeclare metric tokens (the title bar height), so the metrics the geometry reads are taken again under it,
 * once its stylesheet has loaded. Until the store knows that theme, the page keeps the one the boot script put on.
 */

import type { ThemeRef } from "@imbue/workspace-ui/src/themes/themeClient";
import { type ThemeRecord, themeRefOf } from "../model/themes";
import type { DesktopStore } from "../store/DesktopStore";

/** What the page does with the theme shown: its own actions, which a test stands in for. */
export interface ShownThemeActions {
  /** Wear the theme, remembered unless it is only a preview; settles once the page shows it or another replaced it. */
  readonly wear: (theme: ThemeRef, isRemembered: boolean) => Promise<void>;
  /** Settles once the page has loaded: a stylesheet the boot script put on, and the files it imports, included. */
  readonly afterLoad: () => Promise<void>;
  /** Read the metrics off the page and hand them to the store. */
  readonly takeMetrics: () => void;
  readonly setIconTheme: (theme: ThemeRecord) => void;
}

/**
 * Follow the theme the store shows: draw the icons in it, wear it, and take the metrics again once the page shows it.
 * Every notice takes them again, since a theme whose stylesheet failed was read in the standard look and a later
 * notice of the same theme puts the stylesheet back. Answers a function that stops following.
 */
export function followShownTheme(
  store: Pick<DesktopStore, "onShownThemeChanged">,
  actions: ShownThemeActions,
): () => void {
  return store.onShownThemeChanged(({ record, isPreview }) => {
    actions.setIconTheme(record);
    // A preview is worn and not remembered, so a reload while Desktop settings is open comes back in the saved
    // theme; the saved one is remembered again when the preview ends.
    void actions
      .wear(themeRefOf(record), !isPreview)
      .then(actions.afterLoad)
      .then(actions.takeMetrics)
      .catch((error: unknown) => {
        console.error(`[theme] could not take the metrics of the theme ${record.id}`, error);
      });
  });
}
