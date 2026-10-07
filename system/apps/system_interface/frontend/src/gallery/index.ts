/**
 * The theme gallery (docs/system/blueprint/workspace-themes/, section 8.2), served at `/theme-gallery?theme=<id>`:
 * every part of contract 1 in its states, drawn by the shell's own components under the named theme, with every
 * app's icon. It is how a person or an agent judges a theme, and what the browser tests load to check each theme.
 */

import "../style.css";
import m from "mithril";
import { STANDARD_THEME_ID, wearTheme } from "@imbue/workspace-ui/src/themes/themeClient";
import { fetchInventory, fetchThemes } from "../model/api";
import { themeRefOf } from "../model/themes";
import { setIconTheme } from "../views/themeIcons";
import { ThemeGallery, galleryStateOf } from "../views/ThemeGallery";

async function start(): Promise<void> {
  const root = document.getElementById("app");
  if (root === null) return;
  const requested = new URLSearchParams(window.location.search).get("theme") ?? STANDARD_THEME_ID;
  const [catalog, inventory] = await Promise.all([fetchThemes(), fetchInventory()]);
  const state = galleryStateOf(catalog, requested, inventory.apps);
  if (state === null) {
    root.textContent = `There is no theme ${requested}.`;
    return;
  }
  // Shown, not remembered: the gallery shares the shell's origin, whose next load must not boot into this theme.
  await wearTheme(themeRefOf(state.shown), document, { isRemembered: false });
  setIconTheme(state.shown);
  m.mount(root, { view: () => m(ThemeGallery, { state }) });
}

void start().catch((error: unknown) => {
  console.error("[gallery] could not load the themes or the apps", error);
  const root = document.getElementById("app");
  if (root !== null) {
    root.textContent = `The theme gallery could not load: ${error instanceof Error ? error.message : String(error)}`;
  }
});
