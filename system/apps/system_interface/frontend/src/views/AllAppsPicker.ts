/**
 * The rail's "All apps" popover: every app on the machine, in two groups.
 *
 * Pinning an app to a project IS its membership -- an app is pinned exactly
 * when the project's member list holds its `service:<name>` ref, and there is
 * no second pin state anywhere. So the two groups are "Pinned in <Project>"
 * (this project's app members, in member order, which is the order the rail
 * draws its shortcuts in) and "Unpinned" (every other app on the machine).
 * Deliberately UNFILTERED against what is already open: membership is
 * many-to-many, so an app another project shows is an ordinary unpinned row
 * here, and pinning it adds it to the view you are looking at without taking it
 * from anywhere.
 *
 * Clicking a row opens the app. Hovering one reveals a pin toggle, whose two
 * verbs are the only ones there are: pin to this project (add the member) and
 * unpin from it (remove the member). Unpinning never stops the app -- it is the
 * same act as removing any other object from a view. This component only
 * reports the toggle; the rail files it server-side.
 *
 * Everything is the unfiltered view and pins nothing, since every app on the
 * machine already shows there. Under it the popover is one flat list with no
 * headings and no toggles: there is no membership to add or remove.
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
import { appIconMarkup } from "./appIcon";
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

// The size every glyph in this list is drawn at, app icon and generic alike.
const ROW_GLYPH_SIZE = 14;

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

/** The machine's apps, split by whether the active project pins them. */
export interface AppPinPartition {
  pinned: AppEntry[];
  unpinned: AppEntry[];
}

/**
 * Split the machine's apps into the ones a project pins and the ones it does
 * not.
 *
 * `pinnedAppNames` is the project's app members, so the pinned group comes back
 * in member order -- the order the rail draws its shortcuts in, which is what
 * keeps the two lists reading as one thing. A name that addresses no app the
 * machine offers (a member left behind by an app that has since been
 * unregistered) simply drops out. Everything pins nothing and passes no names,
 * so every app lands in `unpinned`.
 */
export function partitionAppsByPin(apps: readonly AppEntry[], pinnedAppNames: readonly string[]): AppPinPartition {
  const appsByName = new Map(apps.map((app) => [app.name, app]));
  const pinnedNames = new Set(pinnedAppNames);
  return {
    pinned: pinnedAppNames.map((name) => appsByName.get(name)).filter((app): app is AppEntry => app !== undefined),
    unpinned: apps.filter((app) => !pinnedNames.has(app.name)),
  };
}

export interface AllAppsPickerAttrs {
  // The active project's display name, for the pinned group's heading. Null
  // under Everything, which pins nothing and gets one flat list instead.
  projectName: string | null;
  // The active project's app members, by service name, in member order. Empty
  // under Everything.
  pinnedAppNames: readonly string[];
  // Open this app in the active view. The rail closes the popover.
  onOpenApp: (app: AppEntry) => void;
  // Pin this app in the active project, or unpin it -- which is to add or
  // remove its member. The popover stays open either way. Never called under
  // Everything, whose rows carry no toggle.
  onTogglePin: (app: AppEntry, wanted: boolean) => void;
}

export function AllAppsPicker(): m.Component<AllAppsPickerAttrs> {
  let filterText = "";

  /** One app. `isPinned` is null under Everything, which pins nothing: the row
   *  still opens the app, it just carries no toggle. */
  function appRow(app: AppEntry, isPinned: boolean | null, attrs: AllAppsPickerAttrs): m.Vnode {
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
          // An app that registered an icon wears it here too; one that did not
          // keeps the generic "opens somewhere" glyph this list has always used.
          m.trust(appIconMarkup(app.icon, ROW_GLYPH_SIZE, icon("external-link", { size: ROW_GLYPH_SIZE }))),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, app.name),
        isPinned === null
          ? null
          : m(
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
                  isPinned
                    ? "Unpins it here only. It keeps running, and stays in every other project showing it."
                    : "Pin it to this project. It joins this project's tabs and its rail shortcuts.",
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
      const projectName = attrs.projectName;
      const groups = partitionAppsByPin(visibleApps, attrs.pinnedAppNames);

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
          : m(
              "div",
              { class: "min-h-0 flex-1 overflow-y-auto" },
              projectName === null
                ? visibleApps.map((app) => appRow(app, null, attrs))
                : [
                    groups.pinned.length === 0 ? null : groupHeading(`Pinned in ${projectName}`),
                    groups.pinned.map((app) => appRow(app, true, attrs)),
                    groups.unpinned.length === 0 ? null : groupHeading("Unpinned"),
                    groups.unpinned.map((app) => appRow(app, false, attrs)),
                  ],
            ),
      ]);
    },
  };
}
