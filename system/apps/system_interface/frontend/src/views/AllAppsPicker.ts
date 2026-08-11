/**
 * "All apps" picker: every app running on this machine, in one flat list.
 *
 * Deliberately UNFILTERED against the active project. Apps are the one kind of
 * tab that may legitimately appear in several projects at once, so an app that
 * is already open -- in another project or in this one -- still shows up here
 * and can be opened again. This is what makes the picker differ from the "+"
 * menu in DockviewWorkspace, which hides apps that already have a tab.
 *
 * Rows mirror the prototype's "On this machine" list: icon, app name, short
 * type label. Selecting a row hands the entry to the parent, which opens it as
 * a panel in the active project (and owns the flag that mounts this picker, so
 * it also closes it) -- the same parent-owns-the-flag idiom as LayoutDialog and
 * the create modals.
 *
 * No update listener is needed: AgentManager redraws after every WebSocket
 * event, so reading ``getApps()`` inside ``view`` picks up an ``apps_updated``
 * broadcast on its own.
 */

import m from "mithril";
import type { AppEntry } from "../models/AgentManager";
import { getApps } from "../models/AgentManager";
import { icon } from "./icons";

// Show the filter box only once scanning the list by eye stops being faster
// than typing.
const FILTER_ROW_THRESHOLD = 8;

// The surrounding chrome UI is not a tab-able app -- opening it would nest the
// whole workspace inside one of its own panels. Same exclusion, for the same
// reason, as ``buildDropdownItems`` in DockviewWorkspace. Note that this is the
// ONLY thing hidden here: unlike that menu, the picker keeps the terminal and
// browser services (this is the surface that makes them reachable) and never
// hides an app just because it already has a tab somewhere.
const CHROME_APP_NAME = "system_interface";

interface AppTypeInfo {
  // The short label shown in the row's type column.
  label: string;
  // Row tooltip, matching the prototype's per-kind description.
  description: string;
}

const DEFAULT_APP_TYPE: AppTypeInfo = { label: "App", description: "A built app running in this machine" };

// The two services that back a dedicated tab type are named as what they
// actually are rather than lumped in as generic apps; everything else the
// workspace registers is a plain app.
const TYPE_BY_APP_NAME: Record<string, AppTypeInfo> = {
  terminal: { label: "Terminal", description: "Shell session running inside this machine" },
  browser: { label: "Browser", description: "Web browser running inside this machine" },
};

function typeForApp(app: AppEntry): AppTypeInfo {
  return TYPE_BY_APP_NAME[app.name] ?? DEFAULT_APP_TYPE;
}

/** Every app the picker can offer, ordered by name. The server's order is an
 *  arbitrary registration order; a browse-and-search list wants a stable,
 *  predictable one. */
export function pickableApps(): AppEntry[] {
  return getApps()
    .filter((app) => app.name !== CHROME_APP_NAME)
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Case-insensitive substring match on the app name. An empty query matches
 *  everything, so the unfiltered list is just the query-less case. */
export function filterApps(apps: AppEntry[], query: string): AppEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return apps;
  return apps.filter((app) => app.name.toLowerCase().includes(needle));
}

interface AllAppsPickerAttrs {
  // Open this app as a panel in the active project. The picker does not close
  // itself -- the parent holds the flag that mounts it, so it closes it here.
  onOpenApp: (app: AppEntry) => void;
  onCancel: () => void;
}

export function AllAppsPicker(): m.Component<AllAppsPickerAttrs> {
  let filterText = "";

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const apps = pickableApps();
      const visibleApps = filterApps(apps, filterText);

      const rows = visibleApps.map((app) => {
        const appType = typeForApp(app);
        return m(
          "div.layout-dialog-item",
          {
            class: "flex items-center gap-2.5",
            title: appType.description,
            onclick() {
              attrs.onOpenApp(app);
            },
          },
          [
            m(
              "span",
              { class: "text-text-faint flex shrink-0 items-center" },
              m.trust(icon("external-link", { size: 14 })),
            ),
            m("span", { class: "flex-1 truncate" }, app.name),
            m("span", { class: "text-text-faint shrink-0 text-xs" }, appType.label),
          ],
        );
      });

      // Distinguish "this machine runs nothing" from "your query matched
      // nothing" -- the fix for each is different.
      const emptyMessage =
        apps.length === 0 ? "No apps are running on this machine." : `No apps match "${filterText.trim()}".`;

      return m(
        "div.custom-url-dialog-overlay",
        {
          onclick(e: MouseEvent) {
            if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
              attrs.onCancel();
            }
          },
        },
        [
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", "All apps"),
              apps.length > FILTER_ROW_THRESHOLD
                ? m("input.custom-url-dialog-input", {
                    type: "text",
                    value: filterText,
                    placeholder: "Filter apps",
                    autofocus: true,
                    oninput(e: InputEvent) {
                      filterText = (e.target as HTMLInputElement).value;
                    },
                    onkeydown(e: KeyboardEvent) {
                      // Enter opens the top match, so filtering down to the app
                      // you want never needs the mouse.
                      if (e.key === "Enter" && visibleApps.length > 0) attrs.onOpenApp(visibleApps[0]);
                      if (e.key === "Escape") attrs.onCancel();
                    },
                  })
                : null,
              m(
                "div.layout-dialog-list",
                rows.length > 0 ? rows : m("div", { class: "text-text-faint px-3 py-2 text-sm" }, emptyMessage),
              ),
              m("div.custom-url-dialog-actions", [
                m("button.custom-url-dialog-cancel", { onclick: attrs.onCancel }, "Close"),
              ]),
            ],
          ),
        ],
      );
    },
  };
}
