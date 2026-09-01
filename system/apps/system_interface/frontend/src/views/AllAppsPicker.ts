/**
 * The rail's "All apps" popover: every app on the machine the active project
 * has not already pinned.
 *
 * Pinning an app to a project IS its membership -- an app is pinned exactly
 * when the project's member list holds its `service:<name>` ref, and there is
 * no second pin state anywhere. A pinned app is already visible in the rail's
 * own shortcut rows (with its own hover-revealed unpin affordance -- see
 * `pinnedAppRow` in Sidebar.ts), so listing it here too would just be the same
 * row twice; this popover exists for the OTHER apps, the ones a visit here is
 * actually for. Deliberately UNFILTERED against what is already open, though:
 * membership is many-to-many, so an app another project shows is an ordinary
 * pinnable row here, and pinning it adds it to the view you are looking at
 * without taking it from anywhere.
 *
 * Clicking a row opens the app. Hovering one reveals a pin toggle that adds
 * the project's membership (there is nothing to unpin from in here -- see
 * above). The popover deliberately stays open afterward so several apps can be
 * pinned in one visit, which means the row just clicked has to disappear from
 * under the pointer: `ROW_FADE_DURATION_MS` keeps it rendered, collapsing and
 * fading, for one transition's worth of time after the click rather than
 * pulling it the instant the project's member list catches up, so neighboring
 * rows ease upward instead of snapping a different app under a pointer that
 * has not moved. This component only reports the toggle; the rail files it
 * server-side, and a failed pin leaves the row back where it was (see the
 * `rows` computation in `view`) rather than losing it.
 *
 * Everything is the unfiltered view and pins nothing -- its rail already
 * shows a fixed shortcut row for every openable app -- so under it the
 * popover has nothing left to list and shows its already-pinned empty state.
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
import { appStoppedDetail, isAppRunning } from "../models/appLiveness";
import { displayNameForMember } from "../models/MemberTitles";
import { memberRef } from "../models/Projects";
import type { ShortcutName } from "../models/Projects";
import { appServiceDisplayName } from "./derived-names";
import { appIconMarkup } from "./components/appIcon";
import { hoverTooltipAttrs } from "./components/hoverTooltip";
import { icon } from "./components/icons";

// Show the filter box only once scanning the list by eye stops being faster
// than typing.
const FILTER_ROW_THRESHOLD = 8;

// Apps this list deliberately never offers, by name -- a DIFFERENT reason
// than `internal` (filtered in `pickableApps` below): each of these has a
// real page, just reached through its own path instead of this one. The
// surrounding chrome UI is not a tab-able app -- opening it would nest the
// whole workspace inside one of its own panels -- and the terminal and
// browser services are fleets with their own shortcut rows, reached by
// creating a session rather than by opening the service. The file viewer
// likewise has its own built-in rail row (the File Viewer shortcut), so its
// backing "files" service listing here too would just be the same app twice.
// Same exclusions the rail's own shortcut list makes.
const HIDDEN_APP_NAMES: ReadonlySet<string> = new Set(["system_interface", "terminal", "browser", "files"]);

// The size every glyph in this list is drawn at, app icon and generic alike.
const ROW_GLYPH_SIZE = 14;

const XMLNS = "http://www.w3.org/2000/svg";

// A pushpin, on the same 24x24 Feather grid as `icons.ts`. It lives here rather
// than in that shared table because pinning is this popover's affordance alone.
const PIN_PATH = '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z"/><line x1="12" y1="14" x2="12" y2="20"/>';

// How long a just-pinned row keeps rendering, collapsing and fading, before it
// actually drops out of the list -- see the module docstring. Matches this
// codebase's own short-transition convention (Sidebar.ts's rail expansion and
// label fades are the same 150ms).
const ROW_FADE_DURATION_MS = 150;

/** Full <svg> string for the pin toggle. Unfilled: a row in this popover is
 *  never already pinned (see the module docstring), so the toggle only ever
 *  offers to pin, never to unpin, and needs no filled "already pinned"
 *  variant. */
function pinIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" width="14" height="14" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PIN_PATH}</svg>`
  );
}

/** Every app the popover can offer, ordered by name. The server's order is an
 *  arbitrary registration order; a browse-and-search list wants a stable,
 *  predictable one. Exported for the rail, whose shortcut list is drawn from
 *  the same set. */
export function pickableApps(): AppEntry[] {
  return getApps()
    .filter((app) => !app.internal && !HIDDEN_APP_NAMES.has(app.name))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** What one app is called: the name the user gave it if it has one, else its
 *  derived name ("File Viewer" for the built-in files service, the registered
 *  name otherwise). Read through the machine-wide title store like every
 *  other surface that names an object (see models/MemberTitles), so an app
 *  renamed on its tab or in the rail reads the same here. */
export function appDisplayName(app: AppEntry): string {
  return displayNameForMember(memberRef("app", app.name), appServiceDisplayName(app.name));
}

/** Case-insensitive substring match on either name an app answers to: the one
 *  it is displayed under, and the one it is registered under. Both, because a
 *  renamed app has to be findable by the name on its row -- and by the service
 *  name the rest of the machine still addresses it by. An empty query matches
 *  everything, so the unfiltered list is just the query-less case. */
export function filterApps(apps: readonly AppEntry[], query: string): AppEntry[] {
  const needle = query.trim().toLowerCase();
  if (needle === "") return [...apps];
  return apps.filter(
    (app) => app.name.toLowerCase().includes(needle) || appDisplayName(app).toLowerCase().includes(needle),
  );
}

/**
 * The apps a project has not already pinned -- what this popover actually
 * lists (see the module docstring for why a pinned one is excluded rather
 * than merely marked). Everything pins nothing and passes no names, so
 * nothing is excluded.
 */
export function unpinnedApps(apps: readonly AppEntry[], pinnedAppNames: readonly string[]): AppEntry[] {
  const pinnedNames = new Set(pinnedAppNames);
  return apps.filter((app) => !pinnedNames.has(app.name));
}

/** One unpinned built-in shortcut, as the rail hands it over to be listed. */
export interface UnpinnedShortcutRow {
  shortcut: ShortcutName;
  label: string;
  // Trusted markup, built by the rail from its own glyph table.
  iconMarkup: string;
  // Null for a shortcut with nothing to start (the file viewer, which no app
  // backs yet): the row still lists, so it can be pinned back, but it does not
  // pretend to open anything.
  isOpenable: boolean;
}

export interface AllAppsPickerAttrs {
  // The active project's display name. Only read to tell whether there is a
  // project to pin into at all (null under Everything, which pins nothing and
  // gets a plain unfiltered list with no toggles instead) -- unlike before,
  // nothing here prints the name, since there is no longer a pinned group's
  // heading to put it in.
  projectName: string | null;
  // The active project's app members, by service name -- which rows this
  // popover excludes (see `unpinnedApps` and the module docstring). Empty
  // under Everything, which excludes nothing.
  pinnedAppNames: readonly string[];
  // Open this app in the active view. The rail closes the popover.
  onOpenApp: (app: AppEntry) => void;
  // Pin this app to the active project -- add its member. The popover stays
  // open afterward so several apps can be pinned in one visit. Never called
  // under Everything, whose rows carry no toggle; always called with `true`,
  // since a row in this list is by construction not already pinned (see the
  // module docstring) -- kept a two-arg shape rather than a plain callback so
  // it lines up with the rail's own `onSetAppPinned`, which this ultimately
  // reaches.
  onTogglePin: (app: AppEntry, wanted: boolean) => void;
  // The rail's built-in shortcut rows this project has unpinned, ready to
  // render here. Their labels and glyphs are the rail's own vocabulary, so the
  // rail hands them over already resolved rather than this file learning what
  // a "chat" row looks like. Empty under Everything, which unpins nothing.
  unpinnedShortcuts: readonly UnpinnedShortcutRow[];
  // Start whatever this shortcut starts -- exactly what its rail row does, the
  // point being that unpinning moves where the row lives and not what it does.
  onOpenShortcut: (shortcut: ShortcutName) => void;
  // Put this shortcut back in the rail.
  onPinShortcut: (shortcut: ShortcutName) => void;
}

export function AllAppsPicker(): m.Component<AllAppsPickerAttrs> {
  let filterText = "";
  // Names clicked to pin this visit, kept rendered a moment past the click so
  // they can collapse and fade instead of vanishing outright -- see
  // ROW_FADE_DURATION_MS and the module docstring. A row leaves this set (and
  // the DOM) once its own timeout fires; if the pin request itself failed, the
  // row is still genuinely unpinned by then and simply renders again at full
  // size on the next redraw, rather than staying lost.
  const fadingNames = new Set<string>();

  /** One unpinned built-in shortcut. Same shape as an app row on purpose: from
   *  here they are both "a starting point this project does not keep in its
   *  rail", and the only difference is which callback the pin goes to. */
  function shortcutRow(row: UnpinnedShortcutRow, attrs: AllAppsPickerAttrs): m.Vnode {
    return m(
      "div",
      {
        key: `shortcut:${row.shortcut}`,
        class:
          "project-rail-app project-rail-app-shortcut group flex h-8 w-full items-center gap-2 px-3 text-left " +
          "transition-all duration-(--dur-base) " +
          (row.isOpenable ? "cursor-pointer text-primary hover:bg-fill-hover" : "text-faint"),
        onclick: row.isOpenable ? () => attrs.onOpenShortcut(row.shortcut) : undefined,
      },
      [
        m("span", { class: "flex shrink-0 items-center text-faint" }, m.trust(row.iconMarkup)),
        m("span", { class: "min-w-0 flex-1 truncate" }, row.label),
        m(
          "button",
          {
            type: "button",
            class:
              "project-rail-pin flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded " +
              "text-faint opacity-0 hover:text-primary focus-visible:opacity-100 " +
              "group-hover:opacity-100",
            "aria-label": `Pin ${row.label}`,
            ...hoverTooltipAttrs("Pin it back to this project's rail. What it opens does not change."),
            onclick: (event: MouseEvent) => {
              // The row underneath starts the thing; the pin toggle must not.
              event.stopPropagation();
              attrs.onPinShortcut(row.shortcut);
            },
          },
          m.trust(pinIcon()),
        ),
      ],
    );
  }

  /** One app row. `isPinnable` is false under Everything, which pins nothing:
   *  the row still opens the app, it just carries no toggle. `isFadingOut`
   *  collapses an already-pinned row out of the list instead of dropping it
   *  outright (see `fadingNames`); it is only ever true when `isPinnable` is,
   *  since Everything's rows never leave this popover's list to begin with. */
  function appRow(app: AppEntry, isPinnable: boolean, isFadingOut: boolean, attrs: AllAppsPickerAttrs): m.Vnode {
    // What the row reads, and what its control is labeled after: an app named
    // by the user is named that here too, or this popover would be the one
    // surface still calling it by its registration. The row's `key` stays the
    // service name -- that is its identity, and a rename must not remount it.
    const label = appDisplayName(app);
    // A stopped app stays listed (identity is not liveness) but reads dimmed,
    // with the tooltip saying why it is not answering.
    const isStopped = !isAppRunning(app);
    return m(
      "div",
      {
        key: app.name,
        class:
          "project-rail-app group flex w-full items-center gap-2 px-3 text-left " +
          (isStopped ? "project-rail-app-stopped text-faint " : "text-primary ") +
          "transition-all duration-(--dur-base) " +
          (isFadingOut ? "h-0 overflow-hidden opacity-0" : "h-8 cursor-pointer opacity-100 hover:bg-fill-hover"),
        ...(isStopped && !isFadingOut ? hoverTooltipAttrs(`${label} — ${appStoppedDetail(app)}`) : {}),
        onclick: isFadingOut ? undefined : () => attrs.onOpenApp(app),
      },
      [
        m(
          "span",
          { class: "flex shrink-0 items-center text-faint" },
          // An app that registered an icon wears it here too; one that did not
          // keeps the generic "opens somewhere" glyph this list has always used.
          m.trust(appIconMarkup(app.icon, ROW_GLYPH_SIZE, icon("external-link", { size: ROW_GLYPH_SIZE }), app.name)),
        ),
        m("span", { class: "min-w-0 flex-1 truncate" }, label),
        !isPinnable || isFadingOut
          ? null
          : m(
              "button",
              {
                type: "button",
                class:
                  "project-rail-pin flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded " +
                  "text-faint opacity-0 hover:text-primary focus-visible:opacity-100 " +
                  "group-hover:opacity-100",
                "aria-label": `Pin ${label}`,
                ...hoverTooltipAttrs("Pin it to this project. It joins this project's tabs and its rail shortcuts."),
                onclick: (event: MouseEvent) => {
                  // The row underneath opens the app; the pin toggle must not.
                  event.stopPropagation();
                  fadingNames.add(app.name);
                  attrs.onTogglePin(app, true);
                  setTimeout(() => {
                    fadingNames.delete(app.name);
                    m.redraw();
                  }, ROW_FADE_DURATION_MS);
                },
              },
              m.trust(pinIcon()),
            ),
      ],
    );
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const apps = pickableApps();
      const visibleApps = filterApps(apps, filterText);
      const projectName = attrs.projectName;
      // What actually renders: under a project, every match the project has
      // not pinned. Under Everything, nothing -- its rail already shows a
      // fixed row for every openable app, so this popover has only its
      // already-pinned empty state to offer there. A name just clicked to pin
      // is held back out of the exclusion for one transition's worth of time
      // (`fadingNames`), so its row stays put and fades rather than going the
      // instant the project's member list catches up.
      const rows =
        projectName === null
          ? []
          : unpinnedApps(
              visibleApps,
              attrs.pinnedAppNames.filter((name) => !fadingNames.has(name)),
            );
      // The unpinned built-in rows list above the apps: they are the machine's
      // own starting points, and they are what a visit here is for when one of
      // them has been put away. Filtered on the same query, so a search reaches
      // everything the popover offers rather than only half of it.
      const needle = filterText.trim().toLowerCase();
      const shortcutRows = attrs.unpinnedShortcuts.filter(
        (row) => needle === "" || row.label.toLowerCase().includes(needle),
      );

      // Three distinct reasons the list can be empty, each with its own fix:
      // the machine runs nothing, the query matched nothing, or (new now that
      // a pinned app's row is excluded rather than merely marked) everything
      // the query left is already pinned here. Told apart by what the filter
      // itself returned rather than by whether a query was typed -- a query
      // that matches only pinned apps HAS matched, and saying otherwise sends
      // the user looking for an app already sitting in the rail.
      const query = filterText.trim();
      // The machine-runs-nothing case alone gets a second line: unlike the
      // filter misses, its fix is not a different query but building an app,
      // and a fresh workspace's user has no other hint that asking the mind
      // is how apps come to exist.
      const emptyMessage: m.Children =
        apps.length === 0
          ? [
              m("p", "No apps are running on this machine."),
              m("p", { class: "mt-2" }, "Tell Minds to create one via chat!"),
            ]
          : visibleApps.length === 0
            ? `No apps match "${query}".`
            : query === ""
              ? "Every app on this machine is already pinned here."
              : `Every app matching "${query}" is already pinned here.`;

      return m("div", { class: "flex max-h-[60vh] w-[240px] flex-col" }, [
        apps.length > FILTER_ROW_THRESHOLD
          ? m("input", {
              type: "text",
              class:
                "mx-3 my-1 h-7 shrink-0 rounded-md bg-sidebar px-2 text-[13px] text-primary outline-none " +
                "placeholder:text-faint",
              value: filterText,
              placeholder: "Filter apps",
              autofocus: true,
              oninput(event: InputEvent) {
                filterText = (event.target as HTMLInputElement).value;
              },
              onkeydown(event: KeyboardEvent) {
                // Enter opens the top match, so filtering down to the app you
                // want never needs the mouse. Reads off `rows` rather than
                // `visibleApps`: a pinned app's row is not shown here (it
                // already has one in the rail), so Enter must not reach past
                // what is actually on screen to open it.
                if (event.key !== "Enter") return;
                // The top row on screen, which may be a built-in one now that
                // those list here too -- Enter must open what the eye is on.
                const topShortcut = shortcutRows[0];
                if (topShortcut !== undefined) {
                  if (topShortcut.isOpenable) attrs.onOpenShortcut(topShortcut.shortcut);
                  return;
                }
                if (rows.length > 0) attrs.onOpenApp(rows[0]);
              },
            })
          : null,
        rows.length === 0 && shortcutRows.length === 0
          ? m("div", { class: "px-3 py-2 text-[13px] text-faint" }, emptyMessage)
          : m("div", { class: "min-h-0 flex-1 overflow-y-auto" }, [
              ...shortcutRows.map((row) => shortcutRow(row, attrs)),
              ...rows.map((app) => appRow(app, projectName !== null, fadingNames.has(app.name), attrs)),
            ]),
      ]);
    },
  };
}
