/**
 * The theme gallery (docs/system/blueprint/workspace-themes/, section 8.2), served at `/theme-gallery?theme=<id>`:
 * every part of contract 1 in its states, drawn by the shell's own components under the named theme, with every
 * app's icon. It is how a person or an agent judges a theme, and what the browser tests load to check each theme.
 */

import "../style.css";
import m from "mithril";
import { connectToShell } from "@imbue/workspace-ui/src/app_contract";
import { STANDARD_THEME_ID, wearTheme } from "@imbue/workspace-ui/src/themes/themeClient";
import { fetchInventory, fetchThemes } from "../model/api";
import { themeRefOf } from "../model/themes";
import { afterPageLoad } from "../theme/pageLoad";
import { setIconTheme } from "../views/themeIcons";
import { FailureLine } from "../views/FailureLine";
import { ThemeGallery, galleryFailureNotice, galleryStateOf } from "../views/ThemeGallery";

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
  // A theme the boot script put on counts as worn once its bundle parses, before the files it imports load; the
  // page's load event waits for those, so the terminal section reads the theme's tokens.
  await afterPageLoad();
  setIconTheme(state.shown);
  m.mount(root, { view: () => m(ThemeGallery, { state }) });
}

void start().catch((error: unknown) => {
  console.error("[gallery] could not load the themes or the apps", error);
  const root = document.getElementById("app");
  if (root === null) return;
  // A shell that frames the gallery says whether Imbue Studio opens bug reports, which can arrive after the failure.
  const connection = connectToShell({ onHandshake: () => m.redraw() });
  const reload = (): void => window.location.reload();
  m.mount(root, {
    view: () =>
      m(FailureLine, {
        notice: galleryFailureNotice(error, reload, connection.canReportBug()),
        onReportBug: connection.canReportBug() ? (failure) => connection.reportBug(failure) : null,
        marker: "theme-gallery-failure",
        messageClass: "text-primary",
      }),
  });
});
