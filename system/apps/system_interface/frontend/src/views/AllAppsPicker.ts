/**
 * The rail's "All apps" popover: every app on the machine, in two groups.
 *
 * The active view's own apps come first and everything else on the machine
 * follows, de-duped, so the view you are in is never buried under the rest of
 * the machine. Deliberately UNFILTERED against what is already open or already
 * on the rail: membership is many-to-many, so an app another project shows is
 * an ordinary row here, and opening it from this list adds it to the view you
 * are looking at without taking it from anywhere.
 *
 * Clicking a row opens the app. Hovering one reveals a pin toggle that adds it
 * to (or takes it off) this view's shortcut list -- which is why the view's own
 * apps still appear here even when they are already shortcuts: unpinning one
 * has to leave it somewhere it can be pinned again. Pin state itself belongs to
 * the rail (see Sidebar's `shortcutAppNames` / `togglePins`); this component
 * only reports the toggle.
 *
 * The popover renders as a bare card and is placed by the rail, which owns the
 * one floating-menu placement (flip, clamp) every one of its menus uses. It
 * does not close itself either: the rail holds the flag that mounts it, closes
 * it when an app is opened, and keeps it open while apps are being pinned.
 *
 * No update listener is needed: AgentManager redraws after every WebSocket
 * event, so reading `getApps()` inside `view` picks up an `apps_updated`
 * broadcast on its own.
 */

import m from "mithril";
import type { AppEntry } from "../models/AgentManager";
import { getApps } from "../models/AgentManager";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";

// Show the filter box only once scanning the list by eye stops being faster
// than typing.
const FILTER_ROW_THRESHOLD = 8;

// Apps this list deliberately never offers. The surrounding chrome UI is not a
// tab-able app -- opening it would nest the whole workspace inside one of its
// own panels -- and the terminal and browser services are fleets with their own
// shortcut rows, reached by creating a session rather than by opening the
// service. Same exclusions the rail's own shortcut list makes.
const HIDDEN_APP_NAMES: ReadonlySet<string> = new Set(["system_interface", "terminal", "browser"]);

const XMLNS = "http://www.w3.org/2000/svg";

// A pushpin, on the same 24x24 Feather grid as `icons.ts`. It lives here rather
// than in that shared table because pinning is this popover's affordance alone.
const PIN_PATH = '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z"/><line x1="12" y1="14" x2="12" y2="20"/>';

/** Full <svg> string for the pin toggle. A pinned app's pin is filled, so the
 *  state reads at a glance rather than only from the tooltip. */
function pinIcon(isPinned: boolean): string {
  return (
    `<svg xmlns="${XMLNS}" width="14" height="14" viewBox="0 0 24 24" ` +
    `fill="${isPinned ? "currentColor" : "none"}" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PIN_PATH}</svg>`
  );
}

/** Every app the popover can offer, ordered by name. The server's order is an
 *  arbitrary registration order; a browse-and-search list wants a stable,
 *  predictable one. Exported for the rail, whose shortcut list is drawn from
 *  the same set. */
export function pickableApps(): AppEntry[] {
  return getApps()
    .filter((app) => !HIDDEN_APP_NAMES.has(app.name))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Case-insensitive substring match on the app name. An empty query matches
 *  everything, so the unfiltered list is just the query-less case. */
export function filterApps(apps: readonly AppEntry[], query: string): AppEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...apps];
  return apps.filter((app) => app.name.toLowerCase().includes(needle));
}

/**
 * Split the machine's apps into the active view's and the rest, putting the
 * view's currently-unpinned apps at the top of its group.
 *
 * Unpinned defaults lead because they are the only apps with nowhere else to be
 * re-pinned from: everything else in this group is already a shortcut, and the
 * rest of the machine is one scroll away either way.
 */
export function groupApps(
  apps: readonly AppEntry[],
  viewAppNames: readonly string[],
  shortcutAppNames: readonly string[],
): { inView: AppEntry[]; onMachine: AppEntry[] } {
  const shown = new Set(viewAppNames);
  const shortcuts = new Set(shortcutAppNames);
  const inView = apps.filter((app) => shown.has(app.name));
  return {
    inView: [...inView.filter((app) => !shortcuts.has(app.name)), ...inView.filter((app) => shortcuts.has(app.name))],
    onMachine: apps.filter((app) => !shown.has(app.name)),
  };
}

export interface AllAppsPickerAttrs {
  // The active view's display name, for the group heading.
  viewName: string;
  // Apps the active view shows, by service name.
  viewAppNames: readonly string[];
  // Apps currently on the rail's shortcut list, by service name.
  shortcutAppNames: readonly string[];
  // Open this app in the active view. The rail closes the popover.
  onOpenApp: (app: AppEntry) => void;
  // Put this app on the rail's shortcut list, or take it off. The popover
  // stays open either way.
  onTogglePin: (app: AppEntry, wanted: boolean) => void;
}

export function AllAppsPicker(): m.Component<AllAppsPickerAttrs> {
  let filterText = "";

  function appRow(app: AppEntry, isPinned: boolean, attrs: AllAppsPickerAttrs): m.Vnode {
    return m(
      "div",
      {
        key: app.name,
        class:
          "project-rail-app group flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left " +
          "text-text-primary hover:bg-bg-hover",
        onclick: () => attrs.onOpenApp(app),
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center text-text-faint" },
          m.trust(icon("external-link", { size: 14 })),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, app.name),
        m(
          "button",
          {
            type: "button",
            class:
              "project-rail-pin flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded " +
              "hover:text-text-primary focus-visible:opacity-100 group-hover:opacity-100 " +
              (isPinned ? "text-text-secondary opacity-100" : "text-text-faint opacity-0"),
            "aria-pressed": isPinned ? "true" : "false",
            "aria-label": isPinned ? `Unpin ${app.name}` : `Pin ${app.name}`,
            ...hoverTooltipAttrs(
              isPinned ? "Remove from this project's shortcuts" : "Pin to this project's shortcuts",
            ),
            onclick: (event: MouseEvent) => {
              // The row underneath opens the app; the pin toggle must not.
              event.stopPropagation();
              attrs.onTogglePin(app, !isPinned);
            },
          },
          m.trust(pinIcon(isPinned)),
        ),
      ],
    );
  }

  function groupHeading(label: string): m.Vnode {
    return m(
      "div",
      { class: "px-3 pt-2 pb-1 text-[11px] font-semibold tracking-wide text-text-faint uppercase" },
      label,
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const apps = pickableApps();
      const visibleApps = filterApps(apps, filterText);
      const groups = groupApps(visibleApps, attrs.viewAppNames, attrs.shortcutAppNames);
      const shortcuts = new Set(attrs.shortcutAppNames);

      // Distinguish "this machine runs nothing" from "your query matched
      // nothing" -- the fix for each is different.
      const emptyMessage =
        apps.length === 0 ? "No apps are running on this machine." : `No apps match "${filterText.trim()}".`;

      return m("div", { class: "flex max-h-[60vh] w-[240px] flex-col" }, [
        apps.length > FILTER_ROW_THRESHOLD
          ? m("input", {
              type: "text",
              class:
                "mx-3 my-1 h-7 shrink-0 rounded-md bg-bg-sidebar px-2 text-[13px] text-text-primary outline-none " +
                "placeholder:text-text-faint",
              value: filterText,
              placeholder: "Filter apps",
              autofocus: true,
              oninput(event: InputEvent) {
                filterText = (event.target as HTMLInputElement).value;
              },
              onkeydown(event: KeyboardEvent) {
                // Enter opens the top match, so filtering down to the app you
                // want never needs the mouse.
                if (event.key === "Enter" && visibleApps.length > 0) attrs.onOpenApp(visibleApps[0]);
              },
            })
          : null,
        visibleApps.length === 0
          ? m("div", { class: "px-3 py-2 text-[13px] text-text-faint" }, emptyMessage)
          : m("div", { class: "min-h-0 flex-1 overflow-y-auto" }, [
              groups.inView.length === 0 ? null : groupHeading(`In ${attrs.viewName}`),
              groups.inView.map((app) => appRow(app, shortcuts.has(app.name), attrs)),
              groups.onMachine.length === 0 ? null : groupHeading("On this machine"),
              groups.onMachine.map((app) => appRow(app, shortcuts.has(app.name), attrs)),
            ]),
      ]);
    },
  };
}
