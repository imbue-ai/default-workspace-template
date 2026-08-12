/**
 * The project rail: the narrow strip down the left edge of the workspace that
 * says which view you are in and lists everything that view holds.
 *
 * At rest it is a 37px icon strip showing only the view's glyph and the
 * shortcut icons; the pointer entering expands it to a 240px panel that floats
 * over the dock (150ms width transition, labels fading in, overlay elevation
 * shadow). The labels are always in the DOM -- they only transition between
 * `opacity-0` and `opacity-100` -- which is what makes the expansion one
 * width/opacity animation rather than a reflow, and the icons sit in a
 * fixed-width leading box so nothing shifts while it runs. The rail is
 * absolutely positioned inside a 37px slot for the same reason: expanding
 * overlays the dock instead of shoving it sideways every time the mouse passes.
 * It collapses again on pointer leave, but stays open while any of its menus is
 * open, since those extend past its own edge.
 *
 * A **view** is either a project (a filter over the machine's objects plus its
 * own layout) or Everything (the unfiltered view, and the home). The rail draws
 * both identically -- the only differences are that Everything has no settings
 * and that nothing can be removed from it, because it is where an object lives
 * when no project shows it.
 *
 * The rail opens nothing itself. The workspace owns panel creation, membership
 * and every destroy verb, so each row calls back with what the user picked (see
 * SidebarAttrs) and the workspace decides what that means -- including raising
 * the confirmation in front of anything destructive.
 */

import m from "mithril";
import type { AppEntry } from "../models/AgentManager";
import {
  EVERYTHING_VIEW_ID,
  EVERYTHING_VIEW_NAME,
  createProject,
  isEverythingView,
  memberRef,
  searchMembers,
  serviceNameFromRef,
} from "../models/Projects";
import type { MatchRange, MemberKind, ProjectInfo } from "../models/Projects";
import { AllAppsPicker, pickableApps } from "./AllAppsPicker";
import { appIconMarkup, serviceIconMarkup } from "./appIcon";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon } from "./icons";
import { ProjectSettingsModal } from "./ProjectSettingsModal";
import { SQUIGGLE_GLYPHS, compositeSquiggleMarkup, monogramMarkup, squiggleMarkup } from "./squiggles";

/** The tab types the rail can create from scratch, one shortcut row each. Also
 *  the vocabulary the New Tab launcher's "open new" tiles speak, so the two
 *  surfaces offer exactly the same starting points. */
export type QuickAddTabType = "chat" | "files" | "browser" | "terminal";

/**
 * One row of the rail's tab list: a member of the active view, whether or not
 * it currently has a tab.
 *
 * The workspace builds these rather than the rail, because what a row is called
 * and whether it is open are both facts about the live dock and the machine
 * (see DockviewWorkspace, and `buildEverythingMembers` for the unfiltered
 * view). The rail only renders, filters and offers actions on them.
 */
export interface SidebarTabRow {
  // The member ref this row stands for (`chat:<agent-id>`, `terminal:<name>`,
  // `service:<name>`, `service:browser?session=<name>`, `url:<hash>`).
  ref: string;
  kind: MemberKind;
  label: string;
  // Whether the object has a tab in the dock right now. Open rows read as
  // primary text, backgrounded ones (running, just not docked) as tertiary.
  isOpen: boolean;
}

export interface SidebarAttrs {
  // Every project on the machine, as last listed. Everything is never in here
  // (it has no registry entry); the switcher appends it below the divider.
  projects: readonly ProjectInfo[];
  // The mounted view: a project id, or EVERYTHING_VIEW_ID.
  activeViewId: string;
  // The active view's tab list, in the order it should render.
  rows: readonly SidebarTabRow[];
  // Mount another view. The workspace saves the outgoing layout and swaps the
  // dock; the rail persists nothing itself, so this is the whole switch.
  onSelectView: (viewId: string) => void;
  // The project registry changed under the rail (a create, a rename, a
  // delete). The workspace re-lists, so `projects` catches up.
  onProjectsChanged: () => void;
  // A project was just created. The workspace mounts it and starts the one
  // chat it is made with, in that order, so the user lands in a working chat
  // rather than on the launcher an empty view would mount. Separate from
  // `onSelectView` because those two have to be sequenced, which only the
  // workspace can do.
  onProjectCreated: (projectId: string) => void;
  // Create a new object of this kind in the active view. Never called with
  // "files" while no app backs it -- that shortcut renders disabled.
  onOpenTabType: (tabType: QuickAddTabType) => void;
  // Open this machine app in the active view, focusing its tab if it has one.
  onOpenApp: (app: AppEntry) => void;
  // Focus this row's existing tab, or open the object into the active pane.
  onOpenRow: (row: SidebarTabRow) => void;
  // Stop showing this row in the active view. Never offered on Everything:
  // nothing can be removed from the home. The object keeps running and stays
  // in every other project showing it.
  onRemoveFromView: (row: SidebarTabRow) => void;
  // The settings dialog's staged removals, applied on Save. Rejects so the
  // dialog can show the reason instead of the rail alerting behind it.
  onRemoveContentFromView: (ref: string) => Promise<void>;
  // The settings dialog's staged renames, applied by the same Save. False when
  // the object has no tab left to name, which the dialog reports.
  onRenameContentInView: (ref: string, title: string) => boolean;
  // Open the machine's share surface with this app pre-selected.
  onShareApp: (row: SidebarTabRow) => void;
  // Destroy the object behind this row, machine-wide. The workspace confirms
  // first -- it is the half that knows what each kind takes down with it.
  onDeleteFromMachine: (row: SidebarTabRow) => void;
}

const COLLAPSED_CLASS = "w-[37px] border-transparent bg-transparent";
const EXPANDED_CLASS = "w-[240px] rounded-lg border-border bg-surface shadow-lg";

// The rail's own padding, and the leading icon box every row shares. Together
// they put an icon's center at 5 + 27/2 = 18.5px: the middle of the collapsed
// 37px strip, so nothing moves horizontally as the rail expands.
const RAIL_PADDING_CLASS = "p-[5px]";
const ICON_BOX_CLASS = "flex w-[27px] shrink-0 items-center justify-center";

// Full-bleed against the rail's padding, so a divider spans the whole card.
const DIVIDER_CLASS = "-mx-[5px] shrink-0 border-t border-border";

const ROW_CLASS = "flex h-7 w-full shrink-0 cursor-pointer items-center gap-1 rounded-md text-left";

// Menu chrome, settled in the design (§6): a floating card on the primary
// surface with a hairline border, 8px radius and the overlay elevation shadow,
// holding 32px rows of icon + label.
const MENU_CARD_CLASS =
  "project-rail-menu fixed z-50 rounded-lg border border-border bg-surface py-1 text-[13px] text-text-primary";
const MENU_SHADOW_STYLE = "box-shadow: 0 1px 1px 0 rgba(0, 0, 0, 0.08), 0 3px 12px 0 rgba(0, 0, 0, 0.08);";
const MENU_ROW_CLASS =
  "project-rail-menu-item flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left hover:bg-bg-hover";

// Minimum gap between a floating menu and the window edges, matching the
// tooltip's own margin so everything that floats clears the frame alike.
const MENU_MARGIN = 6;

const HEADER_GLYPH_SIZE = 18;
const MENU_GLYPH_SIZE = 16;

// Inner markup for the rail's own glyphs, drawn on the same 24x24 Feather grid
// as `icons.ts`. They live here rather than in that shared table because the
// rail is their only consumer.
const RAIL_PATHS = {
  chat:
    '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7' +
    'a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  files: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  browser:
    '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>' +
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
  app: '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  url:
    '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/>' +
    '<path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  // Dots are filled rather than stroked: at 14px a 1px-radius ring reads as
  // fuzz, so the kebab and the "All apps" ellipsis paint solid.
  kebab:
    '<circle cx="12" cy="5" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/>',
  ellipsis:
    '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
} as const;

type RailIconName = keyof typeof RAIL_PATHS;

const XMLNS = "http://www.w3.org/2000/svg";

/** Full <svg> string for one of the rail's glyphs. */
function railIcon(name: RailIconName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${RAIL_PATHS[name]}</svg>`
  );
}

// What each kind of member is drawn as in the tab list. Browsers and terminals
// are fleets rather than installed apps, so they wear the shortcut glyph that
// creates them; anything else registered as a service wears the app tile.
const ICON_BY_MEMBER_KIND: Record<MemberKind, RailIconName> = {
  chat: "chat",
  browser: "browser",
  terminal: "terminal",
  app: "app",
  url: "url",
};

// The rail draws its rows at 16px, shortcuts and tab list alike.
const ROW_GLYPH_SIZE = 16;

/** The glyph one tab-list row wears: an app's own icon when it registered a
 *  usable one, and the kind's built-in glyph otherwise. Only apps have an icon
 *  of their own -- a chat, a terminal, a browser session and a page are all
 *  drawn by what they are. */
function rowIconMarkup(row: SidebarTabRow): string {
  const fallback = railIcon(ICON_BY_MEMBER_KIND[row.kind], ROW_GLYPH_SIZE);
  if (row.kind !== "app") return fallback;
  return serviceIconMarkup(serviceNameFromRef(row.ref), ROW_GLYPH_SIZE, fallback);
}

const SHORTCUT_ROWS: readonly { tabType: QuickAddTabType; label: string }[] = [
  { tabType: "chat", label: "Chat" },
  { tabType: "files", label: "File Viewer" },
  { tabType: "browser", label: "Browser" },
  { tabType: "terminal", label: "Terminal" },
];

// No file-viewer app exists in the workspace template yet, so this shortcut has
// nothing to open. It stays in the list -- it is one of the design's four
// starting points -- but renders disabled rather than pretending to work.
const FILE_VIEWER_TOOLTIP = "A file viewer is coming to this workspace";

/**
 * Full <svg> string for a view's identity, sized to `size` pixels square.
 *
 * Everything is the multicolor composite (all of the projects at once), while a
 * project is its own squiggle in its own color. A project whose stored glyph
 * index addresses no glyph -- a registry written by another version, or by hand
 * -- has no glyph to draw, so it falls back to the letter monogram rather than
 * silently wearing some other project's squiggle.
 */
function viewIdentityMarkup(project: ProjectInfo | null, size: number): string {
  if (project === null) return compositeSquiggleMarkup(size);
  const isDrawable = Number.isInteger(project.glyph) && project.glyph >= 0 && project.glyph < SQUIGGLE_GLYPHS.length;
  return isDrawable
    ? squiggleMarkup(project.glyph, project.color || null, size)
    : monogramMarkup(project.name, project.color, size);
}

// ---------- Floating menu placement ----------

/** The part of a `DOMRect` a floating menu is placed against. */
export interface MenuAnchor {
  left: number;
  right: number;
  top: number;
  bottom: number;
  width: number;
}

export interface MenuSize {
  width: number;
  height: number;
}

export interface MenuPosition {
  left: number;
  top: number;
}

/**
 * Where a floating menu goes: hanging under its anchor ("below", the switcher
 * under the header) or beside it ("right", the row menus and their flyouts).
 *
 * Either way it flips to the opposite side when it would overflow the window
 * and there is room on the other one, then clamps MENU_MARGIN from the edges --
 * a menu opened near the bottom of a short window still has to be readable, and
 * one that would run off the right edge belongs on the left of its trigger.
 */
export function placeMenu(
  anchor: MenuAnchor,
  size: MenuSize,
  viewport: MenuSize,
  placement: "below" | "right",
): MenuPosition {
  let left = placement === "below" ? anchor.left : anchor.right;
  let top = placement === "below" ? anchor.bottom : anchor.top;
  if (placement === "below") {
    const above = anchor.top - size.height;
    if (top + size.height > viewport.height - MENU_MARGIN && above >= MENU_MARGIN) top = above;
  } else {
    const toLeft = anchor.left - size.width;
    if (left + size.width > viewport.width - MENU_MARGIN && toLeft >= MENU_MARGIN) left = toLeft;
  }
  return {
    left: Math.max(MENU_MARGIN, Math.min(left, viewport.width - MENU_MARGIN - size.width)),
    top: Math.max(MENU_MARGIN, Math.min(top, viewport.height - MENU_MARGIN - size.height)),
  };
}

/** Anchor rect for the element an event fired on. */
function anchorForEvent(event: Event): MenuAnchor {
  return (event.currentTarget as HTMLElement).getBoundingClientRect();
}

/** Anchor rect for the pointer itself, so a right-click menu opens where the
 *  click landed rather than against the whole row. */
function anchorForPointer(event: MouseEvent): MenuAnchor {
  return { left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0 };
}

// ---------- Per-view app shortcuts ----------

/** Which apps a view has pinned onto, or unpinned from, its shortcut list. */
export interface ViewPins {
  // Apps pinned here that the view does not itself show.
  pinned: string[];
  // Apps the view shows that the user took off the shortcut list.
  unpinned: string[];
}

// Pin state is per view and per browser: it is a preference about this rail's
// contents rather than a fact about the project, so it rides in localStorage
// beside the client's other per-browser choices instead of in the registry.
const PINS_STORAGE_KEY = "si-sidebar-app-pins";

function readPins(): Record<string, ViewPins> {
  try {
    const stored = localStorage.getItem(PINS_STORAGE_KEY);
    if (stored === null) return {};
    return JSON.parse(stored) as Record<string, ViewPins>;
  } catch {
    // A value another version (or a hand edit) left unparseable is not worth
    // failing the rail over: start from no pins and let the next write fix it.
    return {};
  }
}

function pinsForView(viewId: string): ViewPins {
  const stored = readPins()[viewId];
  return { pinned: stored?.pinned ?? [], unpinned: stored?.unpinned ?? [] };
}

function writePinsForView(viewId: string, pins: ViewPins): void {
  const all = readPins();
  all[viewId] = pins;
  localStorage.setItem(PINS_STORAGE_KEY, JSON.stringify(all));
}

/**
 * The apps on a view's shortcut list: the ones it shows, minus what the user
 * unpinned, plus what the user pinned from elsewhere on the machine.
 *
 * The view's own apps are the defaults, which is why unpinning is recorded in
 * its own list rather than by rewriting a pinned one -- an app that joins the
 * project later should appear on the rail without anybody pinning it.
 */
export function shortcutAppNames(viewAppNames: readonly string[], pins: ViewPins): string[] {
  const unpinned = new Set(pins.unpinned);
  const shown = new Set(viewAppNames);
  return [...viewAppNames.filter((name) => !unpinned.has(name)), ...pins.pinned.filter((name) => !shown.has(name))];
}

/**
 * Pin or unpin one app for a view, given whether the view shows it anyway.
 *
 * Pinning an app the view already shows just clears its unpin; unpinning one it
 * does not show just drops the explicit pin. Neither list keeps an entry that
 * would have no effect, so the stored preference cannot grow without bound.
 */
export function togglePins(pins: ViewPins, appName: string, isShownByView: boolean, wanted: boolean): ViewPins {
  const pinned = pins.pinned.filter((name) => name !== appName);
  const unpinned = pins.unpinned.filter((name) => name !== appName);
  if (wanted && !isShownByView) pinned.push(appName);
  if (!wanted && isShownByView) unpinned.push(appName);
  return { pinned, unpinned };
}

// ---------- New projects ----------

/** The name a fresh project gets: the first "Project N" nobody is using. A
 *  machine holding n projects always leaves one of "Project 1".."Project n+1"
 *  free, so this settles immediately. */
export function nextProjectName(existingNames: readonly string[]): string {
  const taken = new Set(existingNames.map((name) => name.trim().toLowerCase()));
  let index = 1;
  while (taken.has(`project ${index}`)) index += 1;
  return `Project ${index}`;
}

/** The glyph a fresh project gets: the first unused squiggle, so projects made
 *  without ever opening the settings modal still look distinct. Once all ten
 *  are in use they start repeating, which is the point at which the name and
 *  the color are doing the identifying anyway. */
export function nextGlyphIndex(usedGlyphs: readonly number[]): number {
  const used = new Set(usedGlyphs);
  for (let index = 0; index < SQUIGGLE_GLYPHS.length; index += 1) {
    if (!used.has(index)) return index;
  }
  return usedGlyphs.length % SQUIGGLE_GLYPHS.length;
}

// ---------- Row menus ----------

interface RowMenuItem {
  label: string;
  // Shown in red, and always last: these are the ones that take the object off
  // the machine.
  isDestructive: boolean;
  // Explains a verb whose name overstates it. Only "Remove from project" has
  // one, because "remove" reads like a deletion and this one is not.
  tooltip: string | null;
  run: () => void;
}

/**
 * What the "Remove" group offers for one row.
 *
 * Designed per object type against the verbs that actually exist (§6). Apps and
 * URL tabs get no destroy: nothing in this workspace stops a supervised app or
 * deletes its package, and a URL tab is only ever a panel, so either item would
 * be a button that does nothing. Everything contributes no "remove from
 * project" at all -- it is the home, and an object leaves it only by being
 * destroyed.
 */
function removalItemsForRow(row: SidebarTabRow, isEverything: boolean, attrs: SidebarAttrs): RowMenuItem[] {
  const items: RowMenuItem[] = [];
  if (!isEverything) {
    items.push({
      label: "Remove from project",
      isDestructive: false,
      tooltip: "Hides it here only. It keeps running, and stays in Everything and any other project showing it.",
      run: () => attrs.onRemoveFromView(row),
    });
  }
  if (row.kind === "chat" || row.kind === "terminal" || row.kind === "browser") {
    // One label for all three kinds: what each destroy actually takes down is
    // spelled out in the confirmation the workspace raises, where it matters.
    items.push({
      label: "Delete from this machine",
      isDestructive: true,
      tooltip: null,
      run: () => attrs.onDeleteFromMachine(row),
    });
  }
  return items;
}

/** A row's items above the removal group. Sharing is an app affordance: the
 *  share surface is per registered service. */
function directItemsForRow(row: SidebarTabRow, attrs: SidebarAttrs): RowMenuItem[] {
  if (row.kind !== "app") return [];
  return [{ label: "Share app", isDestructive: false, tooltip: null, run: () => attrs.onShareApp(row) }];
}

// ---------- The component ----------

/** Which floating menu is open, and against what. Only one is ever open: they
 *  all close on an outside press, on Escape, and on picking anything. */
type OpenMenu =
  | { kind: "switcher"; anchor: MenuAnchor }
  | { kind: "header"; anchor: MenuAnchor }
  | { kind: "allApps"; anchor: MenuAnchor }
  | { kind: "row"; anchor: MenuAnchor; ref: string };

export function Sidebar(): m.Component<SidebarAttrs> {
  // Expansion is component state rather than a CSS `:hover` rule because
  // picking a row has to collapse the rail again -- otherwise the pointer is
  // left resting on an expanded rail covering the tab it just opened -- and
  // because an open menu has to hold it open even once the pointer has left.
  let expanded = false;
  let openMenu: OpenMenu | null = null;
  // The "Remove" submenu, anchored to its parent row. Tracked separately so it
  // survives the pointer moving off that row and onto the flyout itself.
  let flyoutAnchor: MenuAnchor | null = null;
  // The project whose settings modal is up, or null while it is closed.
  let settingsProject: ProjectInfo | null = null;
  let searchQuery = "";
  let menuError: string | null = null;
  let rootElement: HTMLElement | null = null;

  function isAnyMenuOpen(): boolean {
    return openMenu !== null || settingsProject !== null;
  }

  /** Close every menu and let the rail collapse. The pointer is usually gone
   *  by the time a menu closes (it was over the menu, which sits outside the
   *  rail's own box), so no further mouseleave is coming to do it. */
  function closeMenus(): void {
    openMenu = null;
    flyoutAnchor = null;
    menuError = null;
    expanded = false;
  }

  // The two document listeners are registered once for the rail's life rather
  // than per menu: several cards can be up at once (a menu and its flyout), and
  // add/remove pairs per card would have the first card removed unregistering
  // the handler the second still needs.
  function handleOutsideMousedown(event: MouseEvent): void {
    if (!isAnyMenuOpen()) return;
    // Every floating card is rendered inside the rail's slot, so one
    // containment test covers the rail and everything hanging off it.
    if (rootElement !== null && !rootElement.contains(event.target as Node)) {
      closeMenus();
      m.redraw();
    }
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || !isAnyMenuOpen()) return;
    // Escape backs out of the step the user just took: the flyout first, then
    // the menu it hangs off.
    if (flyoutAnchor !== null) {
      flyoutAnchor = null;
    } else if (openMenu !== null) {
      closeMenus();
    }
    m.redraw();
  }

  function openMenuAt(next: OpenMenu): void {
    openMenu = next;
    flyoutAnchor = null;
    menuError = null;
  }

  /** Run what a row asked for, then put the rail back to rest. */
  function pick(action: () => void): void {
    action();
    closeMenus();
  }

  async function createNewProject(attrs: SidebarAttrs): Promise<void> {
    const glyph = nextGlyphIndex(attrs.projects.map((project) => project.glyph));
    try {
      const created = await createProject(
        nextProjectName(attrs.projects.map((project) => project.name)),
        SQUIGGLE_GLYPHS[glyph].color,
        glyph,
      );
      closeMenus();
      attrs.onProjectsChanged();
      // A project is made to be worked in, so creating mounts it -- and every
      // project is made with a chat of its own, which opens in place of the
      // launcher an empty view would mount. Both are the workspace's, and it
      // does them in that order, so this hands the id over rather than
      // switching here.
      attrs.onProjectCreated(created.project_id);
    } catch (error) {
      // Keep the switcher open with the reason on it: the retry is one click
      // away, and closing would hide why nothing happened.
      menuError = (error as Error).message;
    }
    m.redraw();
  }

  // ---------- Rail rows ----------

  /** The fading label every rail row shares. `whitespace-nowrap` keeps it
   *  sliding out from under the rail's `overflow-hidden` rather than
   *  rewrapping mid-transition. */
  function railLabel(content: m.Children, extraClass: string): m.Vnode {
    return m(
      "span",
      {
        class:
          `min-w-0 flex-1 truncate pr-1 text-[13px] whitespace-nowrap transition-opacity duration-150 ${extraClass} ` +
          (expanded ? "opacity-100" : "opacity-0"),
      },
      content,
    );
  }

  function header(project: ProjectInfo | null, viewName: string): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class:
          "project-rail-header -mx-[5px] -mt-[5px] flex h-[34px] w-[calc(100%+10px)] shrink-0 cursor-pointer " +
          "items-center gap-1 px-[5px] text-left text-text-primary hover:bg-bg-hover",
        "aria-haspopup": "menu",
        "aria-expanded": openMenu?.kind === "switcher" ? "true" : "false",
        ...hoverTooltipAttrs(viewName),
        onclick: (event: MouseEvent) => {
          if (openMenu?.kind === "switcher") {
            openMenu = null;
            return;
          }
          openMenuAt({ kind: "switcher", anchor: anchorForEvent(event) });
        },
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          // Everything is not a project: it has no name, color or glyph to edit
          // and cannot be deleted, so there is nothing to offer on it.
          if (project === null) return;
          openMenuAt({ kind: "header", anchor: anchorForPointer(event) });
        },
      },
      [
        m("span", { class: ICON_BOX_CLASS }, m.trust(viewIdentityMarkup(project, HEADER_GLYPH_SIZE))),
        railLabel(viewName, "font-semibold"),
        m(
          "span",
          {
            class:
              "flex shrink-0 items-center pr-1 text-text-secondary transition-opacity duration-150 " +
              (expanded ? "opacity-100" : "opacity-0"),
          },
          m.trust(icon("chevron-down", { size: 14 })),
        ),
      ],
    );
  }

  /** One shortcut row. The tooltip rides on a wrapper rather than the button
   *  because a disabled control swallows pointer events in every browser -- and
   *  the disabled case is exactly the one whose tooltip has something to say.
   *  Same shape ProjectSettingsModal's Delete button uses. */
  function shortcutRow(options: {
    key: string;
    iconMarkup: string;
    label: string;
    tooltip: string;
    onclick: (() => void) | null;
  }): m.Vnode {
    const isDisabled = options.onclick === null;
    return m(
      "span",
      { key: options.key, class: "flex w-full shrink-0", ...hoverTooltipAttrs(options.tooltip) },
      m(
        "button",
        {
          type: "button",
          disabled: isDisabled,
          class:
            `project-rail-shortcut ${ROW_CLASS} ` +
            (isDisabled ? "cursor-default text-text-faint opacity-60" : "text-text-primary hover:bg-bg-hover"),
          onclick: options.onclick ?? undefined,
        },
        [m("span", { class: ICON_BOX_CLASS }, m.trust(options.iconMarkup)), railLabel(options.label, "")],
      ),
    );
  }

  function shortcuts(attrs: SidebarAttrs, shortcutApps: readonly AppEntry[]): m.Vnode {
    return m("div", { class: "min-h-0 shrink overflow-x-hidden overflow-y-auto" }, [
      ...SHORTCUT_ROWS.map((row) =>
        shortcutRow({
          key: `tab-type:${row.tabType}`,
          iconMarkup: railIcon(row.tabType, ROW_GLYPH_SIZE),
          label: row.label,
          tooltip: row.tabType === "files" ? FILE_VIEWER_TOOLTIP : row.label,
          onclick: row.tabType === "files" ? null : () => pick(() => attrs.onOpenTabType(row.tabType)),
        }),
      ),
      ...shortcutApps.map((app) =>
        shortcutRow({
          key: `app:${app.name}`,
          iconMarkup: appIconMarkup(app.icon, ROW_GLYPH_SIZE, railIcon("app", ROW_GLYPH_SIZE)),
          label: app.name,
          tooltip: app.name,
          onclick: () => pick(() => attrs.onOpenApp(app)),
        }),
      ),
    ]);
  }

  function allAppsRow(): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        // Quieter than the shortcuts above it: this is the way to the rest of
        // the machine, not one of the machine's own starting points.
        class: `project-rail-all-apps ${ROW_CLASS} text-text-faint hover:bg-bg-hover hover:text-text-secondary`,
        "aria-haspopup": "menu",
        "aria-expanded": openMenu?.kind === "allApps" ? "true" : "false",
        onclick: (event: MouseEvent) => {
          if (openMenu?.kind === "allApps") {
            openMenu = null;
            return;
          }
          openMenuAt({ kind: "allApps", anchor: anchorForEvent(event) });
        },
      },
      [m("span", { class: ICON_BOX_CLASS }, m.trust(railIcon("ellipsis", 16))), railLabel("All apps", "")],
    );
  }

  function searchPill(viewName: string): m.Vnode {
    return m(
      "div",
      { class: "my-1 flex h-7 shrink-0 items-center gap-2 rounded-md bg-bg-sidebar px-2 text-text-faint" },
      [
        m("span", { class: "flex shrink-0 items-center" }, m.trust(railIcon("search", 14))),
        m("input", {
          type: "text",
          class:
            "project-rail-search min-w-0 flex-1 bg-transparent text-[13px] text-text-primary outline-none " +
            "placeholder:text-text-faint",
          placeholder: `Find a tab in ${viewName}`,
          value: searchQuery,
          oninput: (event: InputEvent) => {
            searchQuery = (event.target as HTMLInputElement).value;
          },
          onkeydown: (event: KeyboardEvent) => {
            if (event.key === "Escape") searchQuery = "";
          },
        }),
      ],
    );
  }

  /** A tab-list label with the searched-for substrings picked out. */
  function matchedLabel(label: string, ranges: readonly MatchRange[]): m.Children {
    if (ranges.length === 0) return label;
    const parts: m.Children[] = [];
    let cursor = 0;
    for (const range of ranges) {
      if (range.start > cursor) parts.push(label.slice(cursor, range.start));
      parts.push(m("strong", { class: "font-semibold" }, label.slice(range.start, range.end)));
      cursor = range.end;
    }
    if (cursor < label.length) parts.push(label.slice(cursor));
    return parts;
  }

  function tabRow(row: SidebarTabRow, ranges: readonly MatchRange[], attrs: SidebarAttrs, hasMenu: boolean): m.Vnode {
    const isMenuOpenHere = openMenu?.kind === "row" && openMenu.ref === row.ref;
    return m(
      "div",
      {
        key: row.ref,
        // No persistent selected-row highlight: the dock shows several panes at
        // once, so marking one row as "the" selection would misread. Hover is
        // the only fill.
        class:
          `project-rail-tab group ${ROW_CLASS} pr-1 hover:bg-bg-hover ` +
          (row.isOpen ? "text-text-primary" : "text-text-faint"),
        onclick: () => pick(() => attrs.onOpenRow(row)),
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          if (!hasMenu) return;
          openMenuAt({ kind: "row", anchor: anchorForPointer(event), ref: row.ref });
        },
      },
      [
        m("span", { class: ICON_BOX_CLASS }, m.trust(rowIconMarkup(row))),
        m("span", { class: "min-w-0 flex-1 truncate text-[13px] whitespace-nowrap" }, matchedLabel(row.label, ranges)),
        hasMenu
          ? m(
              "button",
              {
                type: "button",
                class:
                  "flex h-5 w-5 shrink-0 cursor-pointer items-center justify-center rounded text-text-faint " +
                  "hover:text-text-primary focus-visible:opacity-100 group-hover:opacity-100 " +
                  (isMenuOpenHere ? "opacity-100" : "opacity-0"),
                "aria-label": `Actions for ${row.label}`,
                onclick: (event: MouseEvent) => {
                  // The row underneath opens the object; the kebab must not.
                  event.stopPropagation();
                  if (isMenuOpenHere) {
                    openMenu = null;
                    return;
                  }
                  openMenuAt({ kind: "row", anchor: anchorForEvent(event), ref: row.ref });
                },
              },
              m.trust(railIcon("kebab", 14)),
            )
          : null,
      ],
    );
  }

  function tabList(attrs: SidebarAttrs, isEverything: boolean): m.Vnode {
    const results = searchMembers(attrs.rows, searchQuery);
    if (results.length === 0) {
      return m(
        "div",
        { class: "px-2 py-2 text-[13px] text-text-faint" },
        attrs.rows.length === 0 ? "Nothing here yet." : "No tabs match that.",
      );
    }
    return m(
      "div",
      { class: "min-h-0 flex-1 overflow-x-hidden overflow-y-auto" },
      results.map((result) => {
        const row = result.member;
        const hasMenu = directItemsForRow(row, attrs).length + removalItemsForRow(row, isEverything, attrs).length > 0;
        return tabRow(row, result.labelRanges, attrs, hasMenu);
      }),
    );
  }

  // ---------- Floating menus ----------

  /**
   * A floating card, placed against `anchor` once it has been measured.
   *
   * Measuring in `oncreate`/`onupdate` rather than guessing at a size is the
   * same dance hoverTooltip does, and for the same reason: the card's height
   * depends on how many rows it holds, and both the flip and the clamp need it.
   * Those hooks run before paint, so the card is never seen at the origin.
   */
  function floatingCard(options: {
    anchor: MenuAnchor;
    placement: "below" | "right";
    role: string;
    width: number | null;
    children: m.Children;
  }): m.Vnode {
    const place = (vnode: m.VnodeDOM): void => {
      const element = vnode.dom as HTMLElement;
      const rect = element.getBoundingClientRect();
      const position = placeMenu(
        options.anchor,
        { width: rect.width, height: rect.height },
        { width: window.innerWidth, height: window.innerHeight },
        options.placement,
      );
      element.style.left = `${position.left}px`;
      element.style.top = `${position.top}px`;
    };
    return m(
      "div",
      {
        class: MENU_CARD_CLASS,
        role: options.role,
        style: `left: 0; top: 0; ${options.width === null ? "" : `width: ${options.width}px;`} ${MENU_SHADOW_STYLE}`,
        oncreate: place,
        onupdate: place,
      },
      options.children,
    );
  }

  function menuRow(options: {
    iconMarkup: string | null;
    label: string;
    isActive?: boolean;
    isDestructive?: boolean;
    isQuiet?: boolean;
    tooltip?: string | null;
    onclick: (event: MouseEvent) => void;
    onmouseenter?: (event: MouseEvent) => void;
    trailing?: m.Children;
  }): m.Vnode {
    const tone = options.isDestructive
      ? "text-red-600"
      : options.isQuiet
        ? "text-text-faint"
        : options.isActive
          ? "font-semibold text-text-primary"
          : "text-text-primary";
    return m(
      "div",
      {
        class: `${MENU_ROW_CLASS} ${tone}`,
        role: "menuitem",
        ...(options.tooltip === null || options.tooltip === undefined ? {} : hoverTooltipAttrs(options.tooltip)),
        onclick: options.onclick,
        // Moving onto any other item of the parent menu drops the flyout, which
        // is what closes it once the pointer wanders off the "Remove" row.
        onmouseenter:
          options.onmouseenter ??
          (() => {
            flyoutAnchor = null;
          }),
      },
      [
        options.iconMarkup === null
          ? null
          : m("span", { class: "flex w-4 shrink-0 items-center justify-center" }, m.trust(options.iconMarkup)),
        m("span", { class: "min-w-0 flex-1 truncate" }, options.label),
        options.trailing ?? null,
      ],
    );
  }

  function checkMark(): m.Vnode {
    return m("span", { class: "flex shrink-0 items-center" }, m.trust(icon("check", { size: 14 })));
  }

  /** The gear the current project's row carries instead of a checkmark.
   *
   *  Picking the project you are already in does nothing, so that row's click
   *  target is spent: it opens the project's settings instead, which is also
   *  where the row's own name, color and glyph are edited. The gear is a plain
   *  affordance rather than a nested button -- the whole row is the target, so
   *  a button inside it would only add a second thing to aim at. */
  function settingsGear(): m.Vnode {
    return m(
      "span",
      { class: "flex shrink-0 items-center text-text-faint" },
      m.trust(icon("settings", { size: 14, strokeWidth: 1.75 })),
    );
  }

  function switcherMenu(attrs: SidebarAttrs, anchor: MenuAnchor): m.Vnode {
    const isEverythingActive = isEverythingView(attrs.activeViewId);
    return floatingCard({
      anchor,
      placement: "below",
      role: "menu",
      // Sized to the header it hangs off, so the two read as one control.
      width: anchor.width,
      children: [
        attrs.projects.map((project) => {
          const isCurrent = project.project_id === attrs.activeViewId;
          return menuRow({
            iconMarkup: viewIdentityMarkup(project, MENU_GLYPH_SIZE),
            label: project.name,
            isActive: isCurrent,
            trailing: isCurrent ? settingsGear() : null,
            tooltip: isCurrent ? `Project settings for ${project.name}` : null,
            onclick: () =>
              pick(() => {
                if (isCurrent) {
                  settingsProject = project;
                  return;
                }
                attrs.onSelectView(project.project_id);
              }),
          });
        }),
        menuRow({
          iconMarkup: railIcon("plus", 14),
          label: "New project",
          isQuiet: true,
          onclick: () => {
            void createNewProject(attrs);
          },
        }),
        menuError === null ? null : m("div", { class: "px-3 py-1 text-[12px] text-red-600" }, menuError),
        m("div", { class: "my-1 border-t border-border" }),
        // Everything sits below the divider because it is not one of the
        // projects -- it is the unfiltered view they all live inside -- but it
        // is picked exactly like one, and has a dock of its own.
        menuRow({
          iconMarkup: compositeSquiggleMarkup(MENU_GLYPH_SIZE),
          label: EVERYTHING_VIEW_NAME,
          isActive: isEverythingActive,
          trailing: isEverythingActive ? checkMark() : null,
          onclick: () => pick(() => attrs.onSelectView(EVERYTHING_VIEW_ID)),
        }),
      ],
    });
  }

  function headerMenu(project: ProjectInfo, anchor: MenuAnchor): m.Vnode {
    return floatingCard({
      anchor,
      placement: "right",
      role: "menu",
      width: null,
      children: menuRow({
        iconMarkup: null,
        label: "Project settings...",
        onclick: () => {
          openMenu = null;
          settingsProject = project;
        },
      }),
    });
  }

  function rowMenu(attrs: SidebarAttrs, menu: Extract<OpenMenu, { kind: "row" }>, isEverything: boolean): m.Children {
    const row = attrs.rows.find((candidate) => candidate.ref === menu.ref);
    if (row === undefined) return null;
    const direct = directItemsForRow(row, attrs);
    const removal = removalItemsForRow(row, isEverything, attrs);
    return [
      floatingCard({
        anchor: menu.anchor,
        placement: "right",
        role: "menu",
        width: null,
        children: [
          direct.map((item) =>
            menuRow({
              iconMarkup: icon("share", { size: 14 }),
              label: item.label,
              onclick: () => pick(item.run),
            }),
          ),
          direct.length > 0 && removal.length > 0 ? m("div", { class: "my-1 border-t border-border" }) : null,
          // A submenu holding a single verb is just a slower way to reach that
          // verb, so the group only flies out once it has a choice to offer.
          removal.length > 1
            ? menuRow({
                iconMarkup: icon("trash", { size: 14 }),
                label: "Remove",
                trailing: m(
                  "span",
                  { class: "flex shrink-0 items-center text-text-faint" },
                  m.trust(icon("chevron-right", { size: 14 })),
                ),
                onclick: (event: MouseEvent) => {
                  flyoutAnchor = anchorForEvent(event);
                },
                onmouseenter: (event: MouseEvent) => {
                  flyoutAnchor = anchorForEvent(event);
                },
              })
            : removal.length === 1
              ? menuRow({
                  iconMarkup: icon("trash", { size: 14 }),
                  label: removal[0].label,
                  isDestructive: removal[0].isDestructive,
                  tooltip: removal[0].tooltip,
                  onclick: () => pick(removal[0].run),
                })
              : null,
        ],
      }),
      flyoutAnchor === null || removal.length < 2
        ? null
        : floatingCard({
            anchor: flyoutAnchor,
            placement: "right",
            role: "menu",
            width: null,
            children: removal.map((item) =>
              menuRow({
                iconMarkup: null,
                label: item.label,
                isDestructive: item.isDestructive,
                tooltip: item.tooltip,
                onclick: () => pick(item.run),
                // The flyout's own rows must not close the flyout they are in.
                onmouseenter: () => {},
              }),
            ),
          }),
    ];
  }

  function allAppsMenu(attrs: SidebarAttrs, anchor: MenuAnchor, viewName: string, viewAppNames: string[]): m.Vnode {
    return floatingCard({
      anchor,
      placement: "right",
      role: "dialog",
      width: null,
      children: m(AllAppsPicker, {
        viewName,
        viewAppNames,
        shortcutAppNames: shortcutAppNames(viewAppNames, pinsForView(attrs.activeViewId)),
        onOpenApp: (app: AppEntry) => pick(() => attrs.onOpenApp(app)),
        onTogglePin: (app: AppEntry, wanted: boolean) => {
          // Pinning is not picking: the popover stays open so several apps can
          // be pinned in one visit.
          writePinsForView(
            attrs.activeViewId,
            togglePins(pinsForView(attrs.activeViewId), app.name, viewAppNames.includes(app.name), wanted),
          );
        },
      }),
    });
  }

  /** The settings modal, opened from the header's context menu. It re-lists on
   *  the way back out rather than patching the cached registry: the server
   *  normalizes the name, and a delete has to be reconciled against the mounted
   *  view anyway -- which the workspace does off the `project_deleted`
   *  broadcast, the same path another client's delete takes. */
  function settingsModal(attrs: SidebarAttrs): m.Children {
    const project = settingsProject;
    if (project === null) return null;
    const close = (): void => {
      settingsProject = null;
      expanded = false;
    };
    return m(ProjectSettingsModal, {
      project,
      onSaved: () => {
        close();
        attrs.onProjectsChanged();
      },
      onDeleted: () => {
        close();
        attrs.onProjectsChanged();
      },
      onCancel: close,
      // The gear only appears on the row for the view already mounted, so the
      // rail's own rows are that project's contents -- no second fetch, and no
      // way for the two lists to disagree.
      contents: attrs.rows.map((row) => ({
        ref: row.ref,
        kind: row.kind,
        label: row.label,
        isOpen: row.isOpen,
      })),
      onRemoveContent: (ref: string) => attrs.onRemoveContentFromView(ref),
      onRenameContent: (ref: string, title: string) => attrs.onRenameContentInView(ref, title),
    });
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      const isEverything = isEverythingView(attrs.activeViewId);
      const project = attrs.projects.find((candidate) => candidate.project_id === attrs.activeViewId) ?? null;
      const viewName = isEverything ? EVERYTHING_VIEW_NAME : (project?.name ?? "");

      // The app list is whatever AgentManager last heard over the WebSocket.
      // Every handled WS event ends in an `m.redraw()`, so an `apps_updated`
      // push repaints the rail without a subscription of its own.
      const machineApps = pickableApps();
      const shownRefs = new Set(attrs.rows.map((row) => row.ref));
      // An app belongs to this view when the view shows its service ref, built
      // through `memberRef` so the rail never spells the grammar out itself.
      const viewAppNames = machineApps
        .filter((app) => shownRefs.has(memberRef("app", app.name)))
        .map((app) => app.name);
      const shortcutApps = shortcutAppNames(viewAppNames, pinsForView(attrs.activeViewId))
        .map((name) => machineApps.find((app) => app.name === name))
        .filter((app): app is AppEntry => app !== undefined);

      return m(
        "div",
        {
          // The slot reserves the collapsed width in the app's flex row; the
          // rail (and every menu hanging off it) is positioned within it, so
          // expanding overlays the dock instead of resizing it. The slot is
          // deliberately `relative` with no z-index of its own: a stacking
          // context here would cap the settings modal's own z-index at the
          // rail's, and the modal has to clear the whole workspace.
          class: "relative w-[37px] shrink-0",
          oncreate: (slot: m.VnodeDOM) => {
            rootElement = slot.dom as HTMLElement;
            document.addEventListener("mousedown", handleOutsideMousedown);
            document.addEventListener("keydown", handleKeydown);
          },
          onremove: () => {
            rootElement = null;
            document.removeEventListener("mousedown", handleOutsideMousedown);
            document.removeEventListener("keydown", handleKeydown);
          },
          onmouseenter: () => {
            expanded = true;
          },
          onmouseleave: () => {
            // A menu extends past the rail's own box, so the pointer moving
            // onto one must not fold the rail up underneath it.
            if (!isAnyMenuOpen()) expanded = false;
          },
        },
        [
          m(
            "div",
            {
              class:
                "machine-sidebar absolute inset-y-0 left-0 z-20 flex flex-col overflow-hidden border " +
                `${RAIL_PADDING_CLASS} transition-[width] duration-150 ease-out ` +
                (expanded ? EXPANDED_CLASS : COLLAPSED_CLASS),
            },
            [
              header(project, viewName),
              // Collapsed, the rail is only the view's glyph and the shortcut
              // icons: no search, no tab list, no All apps, no dividers.
              expanded ? m("div", { class: `${DIVIDER_CLASS} mb-1` }) : null,
              shortcuts(attrs, shortcutApps),
              expanded ? allAppsRow() : null,
              expanded ? m("div", { class: `${DIVIDER_CLASS} mt-1` }) : null,
              expanded ? searchPill(viewName) : null,
              expanded ? tabList(attrs, isEverything) : null,
            ],
          ),
          openMenu?.kind === "switcher" ? switcherMenu(attrs, openMenu.anchor) : null,
          openMenu?.kind === "header" && project !== null ? headerMenu(project, openMenu.anchor) : null,
          openMenu?.kind === "allApps" ? allAppsMenu(attrs, openMenu.anchor, viewName, viewAppNames) : null,
          openMenu?.kind === "row" ? rowMenu(attrs, openMenu, isEverything) : null,
          settingsModal(attrs),
        ],
      );
    },
  };
}
