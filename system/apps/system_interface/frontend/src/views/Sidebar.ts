/**
 * The machine's icon rail: a narrow strip pinned to the left edge of the
 * workspace that expands over the content while the pointer is on it.
 *
 * Collapsed it is 37px wide and shows icons only; on hover it widens and the
 * labels fade in. The labels are always in the DOM (they only transition
 * between `opacity-0` and `opacity-100`), which is what makes the expansion a
 * single width/opacity animation rather than a reflow -- the same trick the
 * design prototype's `.machine-sidebar` uses, and the reason the rail is
 * absolutely positioned inside a fixed-width slot: it overlays the panels to
 * its right instead of shoving them sideways every time the mouse passes.
 *
 * Everything the rail opens goes into the ACTIVE project. It does not open
 * anything itself -- the workspace owns panel creation, so each row just calls
 * back with what the user picked (see DockviewWorkspace).
 */

import m from "mithril";
import { getApps } from "../models/AgentManager";
import type { AppEntry } from "../models/AgentManager";
import type { ProjectInfo } from "../models/Projects";
import { squiggleMarkup } from "./squiggles";

/** The tab types the rail can create from scratch, one quick-add row each. */
export type QuickAddTabType = "chat" | "files" | "browser" | "terminal";

export interface SidebarAttrs {
  // The active project, or null before the projects registry has loaded.
  project: ProjectInfo | null;
  // Open a new tab of this type in the active project.
  onOpenTabType: (tabType: QuickAddTabType) => void;
  // Open this machine app as a tab in the active project.
  onOpenApp: (app: AppEntry) => void;
}

const COLLAPSED_CLASS = "w-[37px]";
const EXPANDED_CLASS = "w-[200px] shadow-lg";

// Inner markup for the quick-add glyphs, drawn on the same 24x24 Feather grid
// as `icons.ts`. They live here rather than in that shared table because the
// rail is their only consumer.
const QUICK_ADD_PATHS: Record<QuickAddTabType, string> = {
  chat:
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7' +
    'a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  files: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  browser:
    '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
};

const QUICK_ADD_ROWS: ReadonlyArray<{ tabType: QuickAddTabType; label: string }> = [
  { tabType: "chat", label: "Chat" },
  { tabType: "files", label: "File viewer" },
  { tabType: "browser", label: "Browser" },
  { tabType: "terminal", label: "Terminal" },
];

// Apps the rail deliberately does not list: "system_interface" is the
// surrounding chrome rather than something you open in a panel, and
// "terminal" / "browser" already have their own quick-add rows above. These
// are the same three the "+" menu excludes (see DockviewWorkspace).
const HIDDEN_APP_NAMES: ReadonlySet<string> = new Set(["system_interface", "terminal", "browser"]);

const XMLNS = "http://www.w3.org/2000/svg";

/** Full <svg> string for one quick-add glyph. */
function quickAddIcon(tabType: QuickAddTabType, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${QUICK_ADD_PATHS[tabType]}</svg>`
  );
}

/** An app's stand-in icon: its initial in a tinted square. Apps register only
 *  a name, url and label (see AppEntry), so there is no real icon to draw --
 *  and an initial at least stays distinguishable while the rail is collapsed,
 *  where the icon is all the user has to go on. */
function appMonogram(app: AppEntry): m.Vnode {
  const initial = app.name.trim().charAt(0).toUpperCase();
  return m(
    "span",
    {
      class:
        "flex h-[18px] w-[18px] items-center justify-center rounded bg-bg-active " +
        "text-[10px] leading-none font-semibold text-text-secondary",
    },
    initial,
  );
}

interface RailRowOptions {
  key: string;
  icon: m.Children;
  label: string;
  expanded: boolean;
  onclick: () => void;
}

/** One rail row: a fixed-width icon well plus the label that fades in with the
 *  rail. The label keeps its `whitespace-nowrap` so it slides out from under
 *  the rail's `overflow-hidden` rather than rewrapping mid-transition. */
function railRow(options: RailRowOptions): m.Vnode {
  return m(
    "button",
    {
      key: options.key,
      type: "button",
      title: options.label,
      onclick: options.onclick,
      class:
        "flex h-7 w-full shrink-0 cursor-pointer items-center gap-1 rounded-md px-1 text-left " +
        "text-text-secondary hover:bg-bg-hover hover:text-text-primary",
    },
    [
      m("span", { class: "flex w-5 shrink-0 items-center justify-center" }, options.icon),
      m(
        "span",
        {
          class:
            "min-w-0 flex-1 truncate pr-1 text-[13px] whitespace-nowrap transition-opacity duration-150 " +
            (options.expanded ? "opacity-100" : "opacity-0"),
        },
        options.label,
      ),
    ],
  );
}

export function Sidebar(): m.Component<SidebarAttrs> {
  // Expansion is component state rather than a CSS `:hover` rule because
  // picking a row has to collapse the rail again -- otherwise the pointer is
  // left resting on an expanded rail that covers the tab it just opened.
  let expanded = false;

  return {
    view(vnode) {
      const { project, onOpenTabType, onOpenApp } = vnode.attrs;
      // The app list is whatever AgentManager last heard over the WebSocket.
      // Every handled WS event ends in an `m.redraw()`, so an `apps_updated`
      // push repaints the rail without a subscription of its own.
      const apps = getApps().filter((app) => !HIDDEN_APP_NAMES.has(app.name));

      const pick = (action: () => void) => () => {
        action();
        expanded = false;
      };

      return m(
        "div",
        // The slot reserves the collapsed width in the app's flex row; the
        // rail itself is absolute within it, so expanding overlays the
        // workspace instead of resizing it.
        { class: "relative z-20 w-[37px] shrink-0" },
        m(
          "div",
          {
            class:
              "machine-sidebar absolute inset-y-0 left-0 z-20 flex flex-col overflow-hidden border-r " +
              "border-border bg-bg-sidebar p-1 transition-[width] duration-150 ease-out " +
              (expanded ? EXPANDED_CLASS : COLLAPSED_CLASS),
            onmouseenter: () => {
              expanded = true;
            },
            onmouseleave: () => {
              expanded = false;
            },
          },
          [
            // Header: the active project's squiggle and name, full-bleed to
            // the rail's edges (hence the negative margins undoing the p-1).
            m(
              "div",
              {
                class:
                  "-mx-1 -mt-1 flex h-[34px] w-[calc(100%+8px)] shrink-0 items-center gap-1 px-2 text-text-primary",
                title: project?.name ?? "",
              },
              [
                m(
                  "span",
                  { class: "flex w-5 shrink-0 items-center justify-center" },
                  project === null ? null : m.trust(squiggleMarkup(project.glyph, project.color || null, 16)),
                ),
                m(
                  "span",
                  {
                    class:
                      "min-w-0 flex-1 truncate text-left text-[13px] font-semibold whitespace-nowrap " +
                      "transition-opacity duration-150 " +
                      (expanded ? "opacity-100" : "opacity-0"),
                  },
                  project?.name ?? "",
                ),
              ],
            ),
            m("div", { class: "-mx-1 mb-1 shrink-0 border-t border-border" }),
            m(
              "div",
              { class: "shrink-0" },
              QUICK_ADD_ROWS.map((row) =>
                railRow({
                  key: `tab-type:${row.tabType}`,
                  icon: m.trust(quickAddIcon(row.tabType, 16)),
                  label: row.label,
                  expanded,
                  onclick: pick(() => onOpenTabType(row.tabType)),
                }),
              ),
            ),
            apps.length > 0 ? m("div", { class: "-mx-1 my-1 shrink-0 border-t border-border" }) : null,
            m(
              "div",
              { class: "min-h-0 flex-1 overflow-x-hidden overflow-y-auto" },
              apps.map((app) =>
                railRow({
                  key: `app:${app.name}`,
                  icon: appMonogram(app),
                  label: app.name,
                  expanded,
                  onclick: pick(() => onOpenApp(app)),
                }),
              ),
            ),
          ],
        ),
      );
    },
  };
}
