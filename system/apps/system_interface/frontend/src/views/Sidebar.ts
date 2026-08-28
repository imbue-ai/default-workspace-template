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
import { getPrimaryAgentId } from "../base-path";
import { getApps } from "../models/AgentManager";
import type { AppEntry } from "../models/AgentManager";
import { appStoppedDetail, isAppRunning, isAppStoppable } from "../models/appLiveness";
import {
  EVERYTHING_VIEW_ID,
  EVERYTHING_VIEW_NAME,
  appShortcutId,
  chatAgentIdFromRef,
  createProject,
  instanceNameFromRef,
  isEverythingView,
  isShortcutPinned,
  memberRef,
  projectForViewId,
  searchMembers,
  serviceNameFromRef,
  shortcutModeForProject,
} from "../models/Projects";
import type { MatchRange, MemberKind, ProjectInfo, ShortcutMode } from "../models/Projects";
import { AllAppsPicker, appDisplayName, pickableApps } from "./AllAppsPicker";
import type { UnpinnedShortcutRow } from "./AllAppsPicker";
import { appIconMarkup, serviceIconMarkup } from "./appIcon";
import { Button, buttonClass } from "./Button";
import { hoverTooltipAttrs } from "./hoverTooltip";
import type { TooltipPlacement } from "./hoverTooltip";
import { icon } from "./icons";
import { OBJECT_MENU_DIVIDER, objectMenuEntries } from "./objectMenu";
import type { ObjectMenuActions, ObjectMenuKind } from "./objectMenu";
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
  // Why this row's backing app is not running, when it is not: "stopped" for a
  // supervised app, the managed-outside wording otherwise (see
  // models/appLiveness). Absent for a running app and for every non-app kind.
  // A stopped row renders dimmed with this as its tooltip.
  stoppedDetail?: string;
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
  // Pin this app in the active view, or unpin it. Pinning is membership, so
  // this adds or removes the app's member ref and nothing else: unpinning never
  // stops the app and touches no other project. Never called on Everything,
  // which pins nothing.
  onSetAppPinned: (app: AppEntry, isPinned: boolean) => void;
  // Move one built-in shortcut row between this project's rail and its All apps
  // menu. Project-scoped, so it is stored against the project rather than here.
  onSetShortcutPinned: (shortcut: QuickAddTabType, isPinned: boolean) => void;
  // Flip one shortcut's mode for this project (focus <-> new); the id is a
  // built-in name or ``app:<service>``. Never called under Everything, whose
  // shortcut menu offers no flip (there is no entry to store it against).
  onSetShortcutMode: (shortcutId: string, mode: ShortcutMode) => void;
  // Always create a fresh object of this shortcut's kind -- the menu's
  // complementary action while the row is in focus mode.
  onNewOfKind: (shortcutId: string) => void;
  // Focus the most recently used object of this shortcut's kind -- the menu's
  // complementary action while the row is in new mode. Only called while the
  // active view shows one (the entry is disabled otherwise).
  onFocusLastOfKind: (shortcutId: string) => void;
  // Shortcut ids whose create is in flight right now: those rows stand down so
  // a second click cannot start a second object.
  awaitingShortcutIds: ReadonlySet<string>;
  // Focus this row's existing tab, or open the object into the active pane.
  onOpenRow: (row: SidebarTabRow) => void;
  // Reload what this row is showing when it has an open tab; opens it fresh
  // when it is backgrounded, since there is then nothing live to reload.
  onRefreshRow: (row: SidebarTabRow) => void;
  // Name the object behind this row, machine-wide -- the same rename every
  // other naming surface goes through (see renameMemberRef), so a chosen name
  // reaches this row's own tab immediately if it has one open.
  onRenameRow: (row: SidebarTabRow, title: string) => void;
  // Open the machine's share surface with this app pre-selected.
  onShareApp: (row: SidebarTabRow) => void;
  // Open the membership dialog over this row's object (also show it in the
  // chosen projects). The workspace owns the dialog, as it owns the other
  // modals.
  onAddRowToProjects: (row: SidebarTabRow) => void;
  // Stop a chat agent's process (``mngr stop``), reversibly: the object keeps
  // its transcript, name, and memberships. Only ever called for a
  // non-primary chat row.
  onStopRow: (row: SidebarTabRow) => void;
  // Stop or start one app's supervised program (whichever its liveness calls
  // for), reversibly -- the service half of an instance row's menu. Only ever
  // called with a stoppable app's registered service name.
  onServiceLifecycle: (serviceName: string) => void;
  // Stop showing one object in the active view. The object keeps running and
  // stays in Everything and in any other project showing it.
  onRemoveFromView: (row: SidebarTabRow) => void;
  // Destroy the object behind this row, machine-wide (weaker for an app: see
  // objectMenu.ts). The workspace confirms first -- it is the half that knows
  // what each kind takes down with it.
  onDeleteFromMachine: (row: SidebarTabRow) => void;
}

const COLLAPSED_CLASS = "w-[37px] border-transparent bg-transparent";
const EXPANDED_CLASS = "w-[240px] rounded-lg border-default bg-surface shadow-lg";

// The rail's own padding, and the leading icon box every row shares. The box
// is sized to hug ROW_ICON_SIZE (see below) rather than pad it, so the flex
// gap that follows (ROW_CLASS's `gap-1`) is the whole of the visual space
// before a row's label -- a wider box would add its own centering padding on
// top of that gap, which is what used to make the rail read looser than its
// menus. Both numbers are fixed regardless of the rail's own width, so the
// icon does not move as the rail expands -- only the label past it does.
const RAIL_PADDING_CLASS = "p-[5px]";
const ICON_BOX_CLASS = "flex w-[20px] shrink-0 items-center justify-center";

// Full-bleed against the rail's padding, so a divider spans the whole card.
// Color, not the element, is what's conditional on `expanded` at the one call
// site that sits between rows shared by both rail states (see below) -- the
// rule has to keep occupying its height even hidden, or the shortcut rows
// under it shift down by that height the moment it appears.
//
// The hairline color is carried here rather than left to each call site: a
// bare `border-t` in Tailwind v4 draws in `currentColor`, so a divider that
// forgets it renders as a black rule against the rail's text color instead of
// the intended hairline. A call site overrides it (to transparent) rather than
// supplying it.
const DIVIDER_CLASS = "-mx-[5px] shrink-0 border-t border-default";

// The rail's whole type scale, defined once so a new row inherits it rather
// than picking its own size. ROW_TEXT_CLASS is every row's label, rail rows
// and menu rows alike. ROW_ICON_SIZE is what a row's own leading glyph draws
// at -- it identifies what the row IS. ACTION_ICON_SIZE is the smaller size a
// row's trailing controls draw at instead (a kebab, a rename pencil, a pin
// toggle, the switcher's chevron) -- those are secondary to the row, not what
// it is, and stayed a consistent 14px even while the rows around them drifted.
const ROW_TEXT_CLASS = "text-[13px]";
const ROW_ICON_SIZE = 16;
const ACTION_ICON_SIZE = 14;

const ROW_CLASS = "flex h-7 w-full shrink-0 cursor-pointer items-center gap-1 rounded-md text-left";

/** One hover-revealed row action -- every trailing micro-control on a rail or
 *  menu row (unpin, kebab, rename pencil, remove-from-project) is this single
 *  recipe: the shared Button at its xs icon size, ghost variant, hidden until
 *  the row's `group` hover reveals it (or held visible while its own menu is
 *  open), and always stopping propagation so the row underneath never also
 *  fires. Positioning and marker classes ride `extra`. */
function railAction(options: {
  iconMarkup: string;
  label: string;
  tooltip?: string;
  tooltipPlacement?: TooltipPlacement;
  /** Hold the control visible regardless of hover (its own menu is open). */
  isRevealed?: boolean;
  extra?: string;
  onclick: (event: MouseEvent) => void;
}): m.Vnode {
  const reveal =
    (options.isRevealed === true ? "opacity-100" : "opacity-0") + " focus-visible:opacity-100 group-hover:opacity-100";
  return m(
    Button,
    {
      variant: "ghost",
      icon: true,
      xs: true,
      extra: `${reveal} ${options.extra ?? ""}`,
      "aria-label": options.label,
      ...(options.tooltip === undefined ? {} : hoverTooltipAttrs(options.tooltip, options.tooltipPlacement)),
      onclick: (event: MouseEvent) => {
        event.stopPropagation();
        options.onclick(event);
      },
    },
    m.trust(options.iconMarkup),
  );
}

// Menu chrome, settled in the design (§6): a floating card on the primary
// surface with a hairline border, 8px radius and the overlay elevation shadow,
// holding 32px rows of icon + label.
const MENU_CARD_CLASS = `project-rail-menu fixed z-50 rounded-lg border border-default bg-surface py-1 ${ROW_TEXT_CLASS} text-primary`;
const MENU_SHADOW_STYLE = "box-shadow: 0 1px 1px 0 rgba(0, 0, 0, 0.08), 0 3px 12px 0 rgba(0, 0, 0, 0.08);";
// `group` so a row's own trailing controls (the switcher's edit pencil) can
// reveal themselves on `group-hover:`, the same reveal-on-hover pattern the
// tab list's kebab uses. `gap-1` (4px) matches the rail's own rows
// (ROW_CLASS) -- it used to be a looser `gap-2`, which is what made a menu
// row read as less tight than the rail row sitting right above it.
const MENU_ROW_CLASS =
  "project-rail-menu-item group flex h-8 w-full cursor-pointer items-center gap-1 px-3 text-left hover:bg-fill-hover";

// A transparent overlay rendered behind any open menu and above everything
// else. Menus already dismiss on an outside pointerdown (see
// handleOutsideMousedown below), but without something to catch that press it
// falls through to whatever control happens to sit underneath -- activating
// it, and dragging hover state across it on the way. The scrim gives the
// dismissing press somewhere of its own to land, so closing the menu is *all*
// it does.
const MENU_SCRIM_CLASS = "fixed inset-0 z-40";

// Minimum gap between a floating menu and the window edges, matching the
// tooltip's own margin so everything that floats clears the frame alike.
const MENU_MARGIN = 6;

// The switcher dropdown's own width: a touch wider than the expanded rail
// (240px) it hangs off, since a long project name plus its trailing control
// needs a little more room than that. Deliberately not as wide as it used to
// be (280px) -- that 40px of slack is most of what made the dropdown read as
// its own floating thing rather than a continuation of the rail card sitting
// directly below it.
const SWITCHER_MENU_WIDTH = 256;

// The switcher's own project/Everything/New-project rows: the rail's own
// leading inset (RAIL_PADDING_CLASS) rather than a generic menu row's roomier
// `px-3`, paired with the rail's own ICON_BOX_CLASS for the icon slot passed
// alongside it (see switcherMenu). The switcher's card shares the rail card's
// own left edge (its anchor is the header's bounding rect, which is the rail
// card's), so matching the per-row inset too is what lands a project's icon
// and label at the exact x the rail draws its own rows at -- a few px to the
// left of where the generic menu padding put them, and the reason the
// dropdown now reads as sitting on top of the rail rather than beside it.
const SWITCHER_ROW_CLASS =
  "project-rail-menu-item group flex h-8 w-full cursor-pointer items-center gap-1 pl-[5px] pr-3 text-left " +
  "hover:bg-fill-hover";

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
  // A pushpin. Every row that wears this is already pinned (the rail only
  // ever lists pinned apps), so unlike AllAppsPicker's own toggle -- which
  // only ever offers to pin, since a pinned app no longer has a row there at
  // all -- this one carries no unfilled variant: the head is filled to read
  // as "pinned" at a glance, the string stroked underneath it.
  pin:
    '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z" fill="currentColor" stroke="currentColor"/>' +
    '<line x1="12" y1="14" x2="12" y2="20" fill="none" stroke="currentColor"/>',
  // The one-click "remove from project" a menu-less tab row (a legacy url
  // member) keeps, matching the glyph the same verb wears in every other
  // row's menu (icons.ts's `minus-circle`). Deliberately NOT the pushpin
  // above: a pinned app sits in the shortcut strip and reads as pinned, while
  // a tab-list row is simply filed here, and calling that "unpinning" would
  // name a state the row never showed.
  "remove-from-view": '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
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

/** The glyph one tab-list row wears: an app's own icon when it registered a
 *  usable one, and the kind's built-in glyph otherwise. Only apps have an icon
 *  of their own -- a chat, a terminal, a browser session and a page are all
 *  drawn by what they are. */
function rowIconMarkup(row: SidebarTabRow): string {
  const fallback = railIcon(ICON_BY_MEMBER_KIND[row.kind], ROW_ICON_SIZE);
  if (row.kind !== "app") return fallback;
  return serviceIconMarkup(serviceNameFromRef(row.ref), ROW_ICON_SIZE, fallback);
}

const SHORTCUT_ROWS: readonly { tabType: QuickAddTabType; label: string }[] = [
  { tabType: "chat", label: "Chat" },
  { tabType: "files", label: "File Viewer" },
  { tabType: "browser", label: "Browser" },
  { tabType: "terminal", label: "Terminal" },
];

// A workspace built before the dufs file-viewer service shipped registers no
// "files" app, so the shortcut has nothing to open there. It stays in the list
// -- it is one of the design's four starting points -- but renders disabled
// rather than pretending to work.
const FILE_VIEWER_TOOLTIP = "A file viewer is coming to this workspace";

// Copy for the rail's working shortcuts, as designed -- "A agent chat"
// included, not a typo to silently correct.
const SHORTCUT_TOOLTIPS: Record<QuickAddTabType, string> = {
  chat: "A agent chat to work alongside you",
  files: "Browse and manage the files in your workspace",
  browser: "A browser that agents can control on your behalf",
  terminal: "A terminal to run commands in your workspace",
};

/** Whether a registered app backs the File Viewer shortcut. Read through
 *  ``getApps`` rather than ``pickableApps``, which hides "files" the same way
 *  it hides the other services with their own rail rows. */
function isFileViewerBacked(): boolean {
  return getApps().some((app) => app.name === "files");
}

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

/**
 * The apps pinned in a view, by service name and in member order.
 *
 * Pinning an app to a project IS its membership: it is pinned exactly when the
 * project's member list holds its `service:<name>` ref, so the shortcut list is
 * simply the app rows of the view's tab list. There is no second pin state to
 * reconcile, and nothing about pinning is per-browser.
 *
 * Everything pins nothing. It is the unfiltered view -- every app on the
 * machine is already in its tab list -- so shortcutting them all would only
 * duplicate that list down the rail.
 */
export function pinnedAppNamesForView(rows: readonly SidebarTabRow[], isEverything: boolean): string[] {
  if (isEverything) return [];
  return rows.flatMap((row) => {
    if (row.kind !== "app") return [];
    // Only a BARE `service:<name>` member is a pin. An instance ref
    // (`service:<name>?instance=...`) is an object the project shows -- a
    // tab-list row -- and filing one must not drag the app's shortcut into
    // the rail. A fleet ref (`service:browser?session=...`) names no
    // installed app, but its row is not of kind "app" either.
    if (instanceNameFromRef(row.ref) !== null) return [];
    const name = serviceNameFromRef(row.ref);
    return name === null ? [] : [name];
  });
}

// ---------- New projects ----------

/** The name a fresh project gets: the first "Project N" nobody is using.
 *
 *  "Using" covers both halves of a project's identity. The server slugifies
 *  "Project N" to the id \`project-n\`, and a RENAME keeps the id -- that is
 *  what lets a rename never move the content file -- so a starter project
 *  renamed to "Something" still owns \`project-1\`. Checking names alone then
 *  proposes "Project 1" and the create bounces off the id conflict, which is
 *  exactly the error this function exists to make impossible. A machine
 *  holding n projects always leaves one of "Project 1".."Project n+1" free,
 *  so this settles immediately. */
export function nextProjectName(projects: readonly Pick<ProjectInfo, "name" | "project_id">[]): string {
  const takenNames = new Set(projects.map((project) => project.name.trim().toLowerCase()));
  const takenIds = new Set(projects.map((project) => project.project_id));
  let index = 1;
  while (takenNames.has(`project ${index}`) || takenIds.has(`project-${index}`)) index += 1;
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

/**
 * Which shared verb set (objectMenu.ts) a row's kebab/context menu offers, or
 * null for a kind that module has nothing to say about.
 *
 * The four consolidated kinds map straight across; a "url" row (an ad-hoc
 * page, no longer filed as a member going forward -- see objectMenu.ts's own
 * module docstring) gets no menu at all rather than a partial one. That does
 * not strand a legacy url member still on an older project's list: exactly
 * because it has no menu, its row keeps the one-click remove (see `tabRow`),
 * so unfiling it is still one click, it just has no second verb to sit beside.
 */
function objectMenuKindForRow(row: SidebarTabRow): ObjectMenuKind | null {
  return row.kind === "url" ? null : row.kind;
}

// ---------- The component ----------

/** Which floating menu is open, and against what. Only one is ever open: they
 *  all close on an outside press, on Escape, and on picking anything.
 *
 *  A "row" menu opened from a pinned-app SHORTCUT row carries
 *  ``shortcutAppName``: that one row is both an object (the app) and a
 *  shortcut, so its menu is the object verb set, a divider, then the shortcut
 *  group. A tab-list app row (an app the machine no longer offers) has no
 *  shortcut and leaves it unset. */
type OpenMenu =
  | { kind: "switcher"; anchor: MenuAnchor }
  | { kind: "header"; anchor: MenuAnchor }
  | { kind: "allApps"; anchor: MenuAnchor }
  | { kind: "row"; anchor: MenuAnchor; ref: string; shortcutAppName?: string }
  | { kind: "shortcut"; anchor: MenuAnchor; shortcutId: QuickAddTabType };

export function Sidebar(): m.Component<SidebarAttrs> {
  // Expansion is component state rather than a CSS `:hover` rule because
  // picking a row has to collapse the rail again -- otherwise the pointer is
  // left resting on an expanded rail covering the tab it just opened -- and
  // because the "All apps" popover has to hold it open once the pointer has
  // left the rail's own box for the list hanging off it.
  let expanded = false;
  let openMenu: OpenMenu | null = null;
  // The project whose settings modal is up, or null while it is closed.
  let settingsProject: ProjectInfo | null = null;
  // The ref of the row currently showing its inline rename field in place of
  // its label, or null while every row reads as plain text. At most one row
  // renames at a time, the same way at most one menu is ever open.
  let renamingRef: string | null = null;
  // What has been typed into that field so far. Only meaningful while
  // `renamingRef` is set, and cleared with it. The field cannot simply render
  // `row.label` and read the DOM back on commit: mithril reapplies an input's
  // `value` on every redraw (form attributes skip its unchanged-attribute
  // short-circuit, and the input-specific skip only applies when the DOM
  // already holds that exact value), and it auto-redraws after every handler
  // it binds -- this field's own `onkeydown` included. A field whose value did
  // not track what was typed would have each keystroke reverted on the next
  // frame.
  let renameDraft = "";
  // Whether the pointer is inside the rail's slot -- which is to say over the
  // rail card or over any floating card hanging off it, since every one of
  // those is a descendant of the slot. Only read when a rename ends: the
  // `mouseleave` that would have collapsed the rail is swallowed while a
  // rename field is up, and the browser sends exactly one per crossing, so
  // ending the edit has to make good on it (see `endRename`).
  let isPointerOverRail = false;
  let searchQuery = "";
  let menuError: string | null = null;
  let rootElement: HTMLElement | null = null;
  // The view a switch was last rendered for, so a completed switch can force
  // the rail closed as a fallback to the hover-driven collapse: switching
  // rebuilds the rail's own DOM subtree (a fresh element under wherever the
  // pointer already rests), and a brand new element mid-hover gets a native
  // `mouseenter` with no native `mouseleave` to match once the pointer
  // actually leaves -- the browser's hover tracking was never reset for it,
  // so `expanded` stays stuck true with nothing left to flip it back. A
  // completed switch is itself a strong enough signal that the interaction is
  // over; if the pointer genuinely is still over the rail, the very next real
  // mouseenter re-expands it, so this cannot make the rail wrongly collapse
  // out from under an actual hover.
  let lastRenderedViewId: string | null = null;

  function isAnyMenuOpen(): boolean {
    return openMenu !== null || settingsProject !== null;
  }

  /** Close every menu and let the rail collapse. The pointer is usually gone
   *  by the time a menu closes (it was over the menu, which sits outside the
   *  rail's own box), so no further mouseleave is coming to do it. */
  function closeMenus(): void {
    openMenu = null;
    menuError = null;
    expanded = false;
  }

  // The document listeners are registered once for the rail's life rather than
  // per menu, so a menu closing cannot unregister the handler another one still
  // needs.
  function handleOutsideMousedown(event: MouseEvent): void {
    if (!isAnyMenuOpen()) return;
    // Every floating card is rendered inside the rail's slot, so one
    // containment test covers the rail and everything hanging off it.
    if (rootElement !== null && !rootElement.contains(event.target as Node)) {
      closeMenus();
      m.redraw();
    }
  }

  /**
   * Close on the window losing focus, which is how a click into a PANEL is
   * seen from here.
   *
   * Panels are cross-origin iframes, so a press inside one raises no event in
   * this document at all -- the mousedown handler above never runs, and a menu
   * left open would sit over the pane the user just clicked into. The window
   * blurring is the only signal that reaches us, and it covers the rest of the
   * same family (a click into the surrounding minds chrome, tabbing away, the
   * window going to the background).
   */
  function handleWindowBlur(): void {
    // The rail folds up too, not just the menus. It is a hover affordance, and
    // a window that is not focused is not one the pointer is working -- leaving
    // a 240px card over the dock while the user is in another app is the same
    // wrong as leaving a menu there. `isPointerOverRail` is deliberately NOT
    // consulted: this fires exactly when the pointer's whereabouts stopped
    // being knowable, so a `true` left over from the last crossing means
    // nothing here.
    isPointerOverRail = false;
    if (renamingRef !== null) return;
    if (!isAnyMenuOpen() && !expanded) return;
    closeMenus();
    m.redraw();
  }

  /**
   * What a pointer that has left the document does to the rail -- for both
   * listeners that can report one, since `handlePointerLeftWindow` defers here
   * once it has established that its own `mouseout` is a real exit.
   *
   * The exceptions are the slot's own `onmouseleave`'s, for the same reasons:
   * an open menu extends past the rail and is still the thing the user is
   * working, and a row mid-rename would commit a half-typed name on the
   * input's removal. Both still record that the pointer has gone, so whatever
   * ends the edit folds the rail up in place of the leave this swallowed (see
   * `endRename`).
   *
   * KNOWN LIMIT, measured rather than assumed: in the minds desktop app
   * neither listener fires for a cursor that leaves the app window. The
   * workspace is a cross-origin iframe inside the minds chrome, and the OS
   * simply stops delivering mouse events to the window -- nothing synthesizes
   * a final crossing for a child frame to hear. Only `blur` arrives, which is
   * why the rail folds on a click away but not on a cursor merely sliding off.
   *
   * Both listeners stay because the workspace is also served straight to a
   * browser (a share link), where they do fire and are the right answer.
   *
   * Closing the gap needs a signal from OUTSIDE this frame: the top-level
   * chrome page forwarding its own leave, or Electron's main process asking
   * the OS where the cursor is, either one relayed over the embed contract.
   * That was weighed and judged too much machinery for a rail collapse. Do not
   * re-attempt it from in here -- there is no DOM API that answers "where is
   * the pointer now", only events, and the events do not come.
   */
  function handleDocumentPointerLeave(): void {
    isPointerOverRail = false;
    if (openMenu !== null || renamingRef !== null) return;
    closeMenus();
    m.redraw();
  }

  /**
   * Fold the rail up when the pointer leaves the window altogether.
   *
   * The slot's own `onmouseleave` is the normal path and handles every
   * crossing that stays inside the page. It is not guaranteed to arrive when
   * the cursor exits the WINDOW, though -- a fast exit, or an Electron shell
   * (which is what minds is) letting the cursor out without telling the page
   * -- and the rail was then left expanded over the dock with the pointer on
   * another app entirely.
   *
   * `mouseout` with a null `relatedTarget` is what says "gone from this
   * document": moving between two elements always names the one being
   * entered, and entering an iframe names the frame. So this fires only for
   * the case the slot's own handler can miss.
   *
   * That test is the whole of what is specific to `mouseout`; what to do about
   * a pointer that has gone is `handleDocumentPointerLeave`'s, and is stated
   * once, there.
   */
  function handlePointerLeftWindow(event: MouseEvent): void {
    if (event.relatedTarget !== null) return;
    handleDocumentPointerLeave();
  }

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key !== "Escape" || !isAnyMenuOpen()) return;
    closeMenus();
    m.redraw();
  }

  function openMenuAt(next: OpenMenu): void {
    openMenu = next;
    menuError = null;
  }

  /** Run what a row asked for, then close whatever menu it came from.
   *
   *  Does not force the rail to collapse -- unlike `closeMenus`, which is for
   *  the definitive-dismiss paths (outside click, Escape, window blur) where
   *  the pointer is known to be elsewhere. A pick can come from a row sitting
   *  directly in the rail's own (still-rendered) card, where the pointer is
   *  usually still resting after the click; forcing `expanded` false there
   *  used to snap the rail collapsed under a pointer that never left it. The
   *  real `onmouseleave` handler is what collapses it now, whenever the
   *  pointer actually goes. */
  function pick(action: () => void): void {
    action();
    openMenu = null;
    menuError = null;
  }

  /** Commit a row's rename, or drop it silently for an empty or unchanged
   *  name -- the same "not a name" treatment the tab's own inline editor
   *  gives a blank field, so canceling a half-typed edit can never blank a
   *  chosen name out. */
  function commitRename(row: SidebarTabRow, typed: string, attrs: SidebarAttrs): void {
    endRename();
    const title = typed.trim();
    if (title === "" || title === row.label) return;
    attrs.onRenameRow(row, title);
  }

  /** Put a row into its inline rename field, seeded with its current name. */
  function beginRename(row: SidebarTabRow): void {
    renamingRef = row.ref;
    renameDraft = row.label;
  }

  /** Leave a row's inline rename field, whether what was typed is being
   *  committed or dropped.
   *
   *  Folds the rail up when the pointer has already gone. A rename field holds
   *  the rail open through the `mouseleave` that would otherwise collapse it
   *  (see the slot's own handler -- collapsing mid-edit removes the input, and
   *  the native blur-on-removal would file a half-typed name nobody asked to
   *  file), and the browser sends exactly one leave per crossing, so there is
   *  none left to fold the rail up once the edit is over. Conditional on
   *  `isPointerOverRail` rather than unconditional, because a rename just as
   *  easily ends with the pointer still resting on the rail -- Enter without
   *  having moved -- and collapsing there would snap it shut with no
   *  `mouseenter` coming to bring it back. */
  function endRename(): void {
    renamingRef = null;
    renameDraft = "";
    if (!isPointerOverRail) expanded = false;
  }

  /** Whether a row is the primary agent's own chat, which has no destroy verb. */
  function isPrimaryAgentRow(row: SidebarTabRow): boolean {
    const agentId = chatAgentIdFromRef(row.ref);
    return agentId !== null && agentId === getPrimaryAgentId();
  }

  /**
   * The rail's own ``ObjectMenuActions`` for one row -- the half of the shared
   * verb set (objectMenu.ts) that varies by caller.
   *
   * The differences from the tab's own build (``tabMenuEntries`` in
   * DockviewWorkspace): the rail supplies no ``hideTab`` at all (putting a tab
   * away belongs to the tab's menu, as the inline comment below says), and its
   * Rename opens the rail's own inline editor -- which works for a
   * *backgrounded* row too, since the rename is filed by ref rather than
   * against any open panel.
   */
  function railMenuActions(row: SidebarTabRow, attrs: SidebarAttrs): ObjectMenuActions {
    // An instance row's menu carries the instance's own verbs, with the
    // SERVICE's Share and Stop/Start trailing in their own group -- so the
    // service stays reachable from any of its instances, Everything's rows
    // included. A bare app row IS the service, and keeps the flat menu.
    const instanceServiceName =
      row.kind === "app" && instanceNameFromRef(row.ref) !== null ? serviceNameFromRef(row.ref) : null;
    const serviceApp =
      instanceServiceName === null ? undefined : getApps().find((candidate) => candidate.name === instanceServiceName);
    const serviceLabel = serviceApp !== undefined ? appDisplayName(serviceApp) : (instanceServiceName ?? "");
    return {
      refresh: () => attrs.onRefreshRow(row),
      share:
        row.kind === "app" && instanceServiceName === null
          ? // The row already carries the chosen name; the share label follows it
            // rather than the registered service name (see the tab's own build).
            { label: `Share ${row.label}`, run: () => attrs.onShareApp(row) }
          : null,
      serviceGroup:
        instanceServiceName === null
          ? null
          : {
              // Offered even for an instance of an app the machine no longer
              // registers? No: with no registered service there is nothing to
              // share or stop, so the group collapses away and Delete is the
              // row's remaining verb.
              share:
                serviceApp === undefined ? null : { label: `Share ${serviceLabel}`, run: () => attrs.onShareApp(row) },
              lifecycle:
                serviceApp === undefined || !isAppStoppable(serviceApp)
                  ? null
                  : {
                      label: `${isAppRunning(serviceApp) ? "Stop" : "Start"} ${serviceLabel}`,
                      run: () => attrs.onServiceLifecycle(serviceApp.name),
                    },
            },
      rename: () => beginRename(row),
      // The rail never offers this: putting a tab away is what you want while
      // looking AT the tab, and the tab's own menu carries it. From here the
      // row is a thing the project shows, so the verb that belongs to it is
      // taking it out of the project.
      hideTab: null,
      // Opens the workspace's membership dialog over the object; a rail row
      // always has a ref to file, so it is never withheld here.
      addToProjects: () => attrs.onAddRowToProjects(row),
      // Null in Everything, which is the home: an object leaves it only by
      // being destroyed. For an app this is the same act as the row's own pin
      // icon -- pinning IS membership -- so the two agree by construction.
      removeFromProject: isEverythingView(attrs.activeViewId) ? null : () => attrs.onRemoveFromView(row),
      // A chat's reversible process-level verb (``mngr stop``), withheld for
      // the primary agent exactly as its delete is. The other kinds have no
      // separate stop: a terminal's or browser session's process IS the
      // object, and an app's stop lives in its quit slot below.
      stop:
        row.kind === "chat" && !isPrimaryAgentRow(row)
          ? { label: `Stop ${row.label}`, run: () => attrs.onStopRow(row) }
          : null,
      // Withheld for the primary agent, exactly as the tab's own build
      // withholds it: that agent runs the workspace's services, so deleting it
      // would take the machine down. Both surfaces recognize it by id rather
      // than by name, since a chat can be renamed to anything. An app's slot
      // is the reversible service-level Stop/Start instead of a destroy (see
      // ``railQuitSlot``).
      quit: railQuitSlot(row, attrs),
    };
  }

  /** The destructive-slot verb for one rail row, or null when the row has none.
   *
   *  For an app the slot is the reversible, service-level "Stop {name}" (or
   *  "Start {name}" while stopped), and only for an app the workspace can
   *  honestly stop -- supervised and non-essential. The workspace half
   *  (``destroyMemberRow``) reads the same liveness, so label and act agree. */
  function railQuitSlot(
    row: SidebarTabRow,
    attrs: SidebarAttrs,
  ): { label: string; run: () => void; isDestructive?: boolean } | null {
    if (row.kind === "app") {
      // An instance row deletes like every other object -- confirm-gated,
      // leaves every project, the app keeps running. The service verbs sit in
      // the menu's own service group (see railMenuActions).
      if (instanceNameFromRef(row.ref) !== null) {
        return { label: `Delete ${row.label}`, run: () => attrs.onDeleteFromMachine(row) };
      }
      const app = getApps().find((candidate) => candidate.name === serviceNameFromRef(row.ref));
      if (app === undefined || !isAppStoppable(app)) return null;
      return {
        label: `${isAppRunning(app) ? "Stop" : "Start"} ${row.label}`,
        isDestructive: false,
        run: () => attrs.onDeleteFromMachine(row),
      };
    }
    if (isPrimaryAgentRow(row)) return null;
    return { label: `Delete ${row.label}`, run: () => attrs.onDeleteFromMachine(row) };
  }

  /** One row of a shortcut's own menu group (see ``shortcutMenuEntries``). */
  interface ShortcutMenuEntry {
    label: string;
    run: () => void;
    isDisabled?: boolean;
  }

  /** Whether the active view currently shows an object of this shortcut's
   *  kind -- what "Focus last X" needs to be able to act. The built-in
   *  create-backed kinds match by row kind; the file viewer and apps match by
   *  the backing service's ref. */
  function viewShowsKindOf(shortcutId: string, rows: readonly SidebarTabRow[]): boolean {
    if (shortcutId === "chat" || shortcutId === "browser" || shortcutId === "terminal") {
      return rows.some((row) => row.kind === shortcutId);
    }
    const serviceName =
      shortcutId === "files" ? "files" : shortcutId.startsWith("app:") ? shortcutId.slice("app:".length) : null;
    return serviceName !== null && rows.some((row) => serviceNameFromRef(row.ref) === serviceName);
  }

  /**
   * The shortcut group every shortcut row's menu carries: the complementary
   * action, then the mode flip.
   *
   * The complementary action keeps both behaviors reachable whatever mode the
   * row is in: "New X" while it focuses, "Focus last X" while it creates --
   * disabled when the active view shows no X, since offering a focus that
   * would create is exactly the confusion the two modes exist to avoid. The
   * File Viewer omits it entirely while no app backs it. The mode flip
   * persists per project, so Everything (no entry to store against) offers
   * only the complementary action.
   */
  function shortcutMenuEntries(shortcutId: string, baseLabel: string, attrs: SidebarAttrs): ShortcutMenuEntry[] {
    const isEverything = isEverythingView(attrs.activeViewId);
    const project = isEverything ? null : projectForViewId(attrs.projects, attrs.activeViewId);
    const mode = shortcutModeForProject(project, shortcutId);
    const entries: ShortcutMenuEntry[] = [];
    const isFilesUnbacked = shortcutId === "files" && !isFileViewerBacked();
    if (!isFilesUnbacked) {
      if (mode === "focus") {
        entries.push({ label: `New ${baseLabel}`, run: () => attrs.onNewOfKind(shortcutId) });
      } else {
        entries.push({
          label: `Focus last ${baseLabel}`,
          run: () => attrs.onFocusLastOfKind(shortcutId),
          isDisabled: !viewShowsKindOf(shortcutId, attrs.rows),
        });
      }
    }
    if (project !== null) {
      entries.push({
        label: mode === "focus" ? `Change shortcut to "New ${baseLabel}"` : `Change shortcut to "${baseLabel}"`,
        run: () => attrs.onSetShortcutMode(shortcutId, mode === "focus" ? "new" : "focus"),
      });
    }
    return entries;
  }

  /** The floating menu of one built-in shortcut row: the shortcut group, then
   *  Unpin (the same act as the row's pin icon; absent under Everything). */
  function shortcutMenu(attrs: SidebarAttrs, menu: Extract<OpenMenu, { kind: "shortcut" }>): m.Children {
    const rowDefinition = SHORTCUT_ROWS.find((candidate) => candidate.tabType === menu.shortcutId);
    if (rowDefinition === undefined) return null;
    const entries = shortcutMenuEntries(menu.shortcutId, rowDefinition.label, attrs);
    const isEverything = isEverythingView(attrs.activeViewId);
    const canUnpin = !isEverything && projectForViewId(attrs.projects, attrs.activeViewId) !== null;
    if (entries.length === 0 && !canUnpin) return null;
    return floatingCard({
      anchor: menu.anchor,
      placement: "right",
      role: "menu",
      width: null,
      children: [
        ...entries.map((entry) =>
          menuRow({
            iconMarkup: null,
            label: entry.label,
            isDisabled: entry.isDisabled,
            onclick: () => pick(entry.run),
          }),
        ),
        canUnpin
          ? menuRow({
              iconMarkup: null,
              label: "Unpin",
              onclick: () => pick(() => attrs.onSetShortcutPinned(menu.shortcutId, false)),
            })
          : null,
      ],
    });
  }

  async function createNewProject(attrs: SidebarAttrs): Promise<void> {
    const glyph = nextGlyphIndex(attrs.projects.map((project) => project.glyph));
    try {
      const created = await createProject(nextProjectName(attrs.projects), SQUIGGLE_GLYPHS[glyph].color, glyph);
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
          `min-w-0 flex-1 truncate pr-1 ${ROW_TEXT_CLASS} whitespace-nowrap transition-opacity duration-150 ` +
          `${extraClass} ` +
          (expanded ? "opacity-100" : "opacity-0"),
      },
      content,
    );
  }

  /** The pencil the header reveals on hover, for a project view: one press to
   *  its settings. The switcher's per-row pencils cover the same ground, but
   *  only after opening the switcher and finding the mounted row in it --
   *  with a single project that row is the one already on screen, and nothing
   *  else hinted that settings exist at all. A span with the button role
   *  rather than a real button, because it renders inside the header button
   *  and buttons do not nest. */
  function headerEditButton(project: ProjectInfo): m.Vnode {
    const openSettings = (event: Event): void => {
      // Never also the header's own click: the pencil opens settings, not the
      // switcher.
      event.stopPropagation();
      settingsProject = project;
    };
    return m(
      "span",
      {
        role: "button",
        tabindex: 0,
        // The Button recipe via buttonClass -- the escape hatch, since this
        // control lives inside the header <button> and buttons do not nest --
        // plus the same reveal-on-row-hover treatment railAction applies.
        class: buttonClass("ghost", {
          icon: true,
          xs: true,
          extra: "opacity-0 focus-visible:opacity-100 group-hover:opacity-100",
        }),
        "aria-label": "Project settings",
        ...hoverTooltipAttrs("Project settings"),
        onclick: openSettings,
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            openSettings(event);
          }
        },
      },
      m.trust(icon("edit", { size: ACTION_ICON_SIZE, strokeWidth: 1.75 })),
    );
  }

  function header(project: ProjectInfo | null, viewName: string): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class:
          "project-rail-header group -mx-[5px] -mt-[5px] flex h-[34px] w-[calc(100%+10px)] shrink-0 cursor-pointer " +
          "items-center gap-1 px-[5px] text-left text-primary hover:bg-fill-hover",
        "aria-haspopup": "menu",
        "aria-expanded": openMenu?.kind === "switcher" ? "true" : "false",
        // Static, not the current project's name: the header's own label
        // already says which view is mounted, so the tooltip's job is to say
        // what the button does, not repeat that.
        ...hoverTooltipAttrs("Switch projects", "right"),
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
        m("span", { class: ICON_BOX_CLASS }, m.trust(viewIdentityMarkup(project, ROW_ICON_SIZE))),
        railLabel(viewName, "font-semibold"),
        // Everything has no settings to open, so only a project view carries
        // the pencil. It hides with the chevron while the rail is folded --
        // group-hover alone would flash it during the collapse transition.
        project !== null && expanded ? headerEditButton(project) : null,
        m(
          "span",
          {
            class:
              "flex shrink-0 items-center pr-1 text-secondary transition-opacity duration-150 " +
              (expanded ? "opacity-100" : "opacity-0"),
          },
          m.trust(icon("chevron-down", { size: ACTION_ICON_SIZE })),
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
    // The row's mode-independent name ("Chat" while the label reads "New
    // Chat"), for the trailing controls' accessible names.
    baseLabel: string;
    tooltip: string;
    // Render dimmed while staying clickable: the row's backing app is stopped,
    // which the tooltip explains. Distinct from a null `onclick`, which is a
    // row with nothing to open at all.
    isDimmed?: boolean;
    onclick: (() => void) | null;
    // Move this row into the All apps menu. Null where there is nowhere to
    // record that: Everything has no project entry to store it against.
    onUnpin: (() => void) | null;
    // Open this row's own shortcut menu (complementary action, mode flip,
    // unpin). Null for a row with no menu to offer.
    onMenu: ((event: MouseEvent) => void) | null;
    isMenuOpen: boolean;
  }): m.Vnode {
    // Beside the row rather than beneath it: the rail is a vertical list, and a
    // tooltip centered under one row covers the next one down -- which is
    // usually the row being decided against. Every other surface keeps the
    // shell-matched default (see hoverTooltip.ts).
    const isDisabled = options.onclick === null;
    const hasKebab = options.onMenu !== null;
    const hasUnpin = options.onUnpin !== null;
    return m(
      "span",
      {
        key: options.key,
        // The trailing controls read as the right end of the row, not as
        // controls beside it, so the shortcut button keeps the full width and
        // they are laid OVER its right edge. Siblings rather than children
        // because all of these are real buttons and a button inside a button
        // is not markup a browser will keep. (`pinnedAppRow` below reaches the
        // same look from the other side -- a clickable div -- because there
        // the row itself is the target.) The row's hover fill therefore lives
        // on this slot: the button is not an ancestor of the controls, so its
        // own `:hover` would drop the fill the moment the pointer reached one.
        class:
          "project-rail-shortcut-slot group relative flex w-full shrink-0 items-center rounded-md " +
          (options.onclick === null ? "" : "hover:bg-fill-hover"),
        ...hoverTooltipAttrs(options.tooltip, "right"),
        oncontextmenu:
          options.onMenu === null
            ? undefined
            : (event: MouseEvent) => {
                event.preventDefault();
                options.onMenu?.(event);
              },
      },
      [
        m(
          "button",
          {
            type: "button",
            disabled: isDisabled,
            class:
              `project-rail-shortcut ${ROW_CLASS} ` +
              // Padded so a long label fades out before it reaches the
              // trailing controls rather than running underneath them.
              (hasUnpin && hasKebab ? "pr-12 " : hasUnpin || hasKebab ? "pr-7 " : "") +
              (isDisabled
                ? "cursor-default text-faint opacity-60"
                : options.isDimmed === true
                  ? "project-rail-shortcut-stopped text-faint opacity-60"
                  : "text-primary"),
            onclick: options.onclick ?? undefined,
          },
          [m("span", { class: ICON_BOX_CLASS }, m.trust(options.iconMarkup)), railLabel(options.label, "")],
        ),
        options.onUnpin === null
          ? null
          : railAction({
              iconMarkup: railIcon("pin", ACTION_ICON_SIZE),
              label: `Unpin ${options.baseLabel} from this project`,
              extra: "project-rail-shortcut-unpin absolute right-1",
              onclick: () => options.onUnpin?.(),
            }),
        options.onMenu === null
          ? null
          : railAction({
              iconMarkup: railIcon("kebab", ACTION_ICON_SIZE),
              label: `Shortcut options for ${options.baseLabel}`,
              isRevealed: options.isMenuOpen,
              extra: "project-rail-shortcut-menu absolute " + (hasUnpin ? "right-6" : "right-1"),
              onclick: (event) => options.onMenu?.(event),
            }),
      ],
    );
  }

  /** One pinned-app row. Structured like the tab list's own rows (a clickable
   *  `div` with a trailing hover-revealed control) rather than `shortcutRow`'s
   *  plain `button`, since nesting the unpin button inside another button is
   *  not valid markup. The toggle unpins in one click -- pinning an app to a
   *  project IS its membership (see `pinnedAppNamesForView`), so there is no
   *  second pin state to reconcile, clicking it just drops the member ref --
   *  which today the All apps popover is the only other place to do. Gated on
   *  `expanded` the same as the tab list's own trailing controls: collapsed,
   *  the row is icon-only and has nothing to reveal a control onto. */
  function pinnedAppRow(app: AppEntry, attrs: SidebarAttrs): m.Vnode {
    // An app renamed anywhere is renamed here too: the shortcut and the All
    // apps popover are two views of one object, so they read the one definition
    // of what it is called rather than each keeping its own.
    const label = appDisplayName(app);
    // The label follows the shortcut's mode, exactly as the built-in rows':
    // "New Docs" while the shortcut always opens another pane.
    const appProject = isEverythingView(attrs.activeViewId)
      ? null
      : projectForViewId(attrs.projects, attrs.activeViewId);
    const shownLabel = shortcutModeForProject(appProject, appShortcutId(app.name)) === "new" ? `New ${label}` : label;
    // A stopped app's row stays -- identity is not liveness -- but reads
    // dimmed, and its tooltip says why it is not answering.
    const isStopped = !isAppRunning(app);
    // This row is now the app's ONLY place in the rail (the tab list no longer
    // repeats it), so the app's verbs have to be reachable from here or they
    // would only exist while it happened to have a tab open. The row it stands
    // for is still in `attrs.rows`; it is simply not listed below.
    const appRef = memberRef("app", app.name);
    const memberRow = attrs.rows.find((row) => row.ref === appRef) ?? null;
    const isMenuOpenHere = openMenu?.kind === "row" && openMenu.ref === appRef;
    return m(
      "span",
      {
        key: `app:${app.name}`,
        class: "flex w-full shrink-0",
        ...hoverTooltipAttrs(isStopped ? `${label} — ${appStoppedDetail(app)}` : label, "right"),
      },
      m(
        "div",
        {
          class:
            `project-rail-shortcut group ${ROW_CLASS} pr-1 hover:bg-fill-hover ` +
            (isStopped ? "project-rail-shortcut-stopped text-faint opacity-60" : "text-primary"),
          onclick: () => pick(() => attrs.onOpenApp(app)),
          oncontextmenu: (event: MouseEvent) => {
            event.preventDefault();
            if (memberRow === null) return;
            openMenuAt({ kind: "row", anchor: anchorForPointer(event), ref: appRef, shortcutAppName: app.name });
          },
        },
        [
          m(
            "span",
            { class: ICON_BOX_CLASS },
            m.trust(appIconMarkup(app.icon, ROW_ICON_SIZE, railIcon("app", ROW_ICON_SIZE), app.name)),
          ),
          railLabel(shownLabel, ""),
          // Everything's app rows are fixed -- every openable app shows there
          // by construction, with no registry entry to record an unpin
          // against -- so only a project's rows carry the toggle.
          expanded && !isEverythingView(attrs.activeViewId)
            ? railAction({
                iconMarkup: railIcon("pin", ACTION_ICON_SIZE),
                label: `Unpin ${label}`,
                tooltip: "Unpins it here only. It keeps running, and stays in every other project showing it.",
                tooltipPlacement: "right",
                extra: "project-rail-pin",
                onclick: () => attrs.onSetAppPinned(app, false),
              })
            : null,
          // The app's own verbs -- Refresh, Share, Quit -- reached the same way
          // a tab-list row's are. Absent when the row is not in this view's
          // members, since there is then no object here to act on.
          expanded && memberRow !== null
            ? railAction({
                iconMarkup: railIcon("kebab", ACTION_ICON_SIZE),
                label: `Actions for ${label}`,
                isRevealed: isMenuOpenHere,
                onclick: (event) => {
                  if (isMenuOpenHere) {
                    openMenu = null;
                    return;
                  }
                  openMenuAt({ kind: "row", anchor: anchorForEvent(event), ref: appRef, shortcutAppName: app.name });
                },
              })
            : null,
        ],
      ),
    );
  }

  /** The built-in rows this project has put away, ready for the All apps menu.
   *  Empty under Everything, which has no project entry to unpin against. */
  function unpinnedShortcutRows(attrs: SidebarAttrs): UnpinnedShortcutRow[] {
    if (isEverythingView(attrs.activeViewId)) return [];
    const project = projectForViewId(attrs.projects, attrs.activeViewId);
    if (project === null) return [];
    return SHORTCUT_ROWS.filter((row) => !isShortcutPinned(project, row.tabType)).map((row) => ({
      shortcut: row.tabType,
      label: row.label,
      iconMarkup: railIcon(row.tabType, ROW_ICON_SIZE),
      // The file viewer opens only where a "files" app backs it (a workspace
      // built before the dufs service shipped has none); elsewhere its row
      // lists (to be pinned back) without pretending otherwise.
      isOpenable: row.tabType !== "files" || isFileViewerBacked(),
    }));
  }

  function shortcuts(attrs: SidebarAttrs, shortcutApps: readonly AppEntry[]): m.Vnode {
    // Everything has no project entry, so there is nowhere to record an unpin
    // or a mode and all four rows stay on the defaults: it is the home, and it
    // shows every starting point the machine has.
    const isEverything = isEverythingView(attrs.activeViewId);
    const project = isEverything ? null : projectForViewId(attrs.projects, attrs.activeViewId);
    const canUnpin = !isEverything && project !== null;
    return m("div", { class: "min-h-0 shrink overflow-x-hidden overflow-y-auto" }, [
      ...SHORTCUT_ROWS.filter((row) => !canUnpin || isShortcutPinned(project, row.tabType)).map((row) => {
        const isDisabledFilesRow = row.tabType === "files" && !isFileViewerBacked();
        // A backed-but-stopped file viewer stays clickable (opening it shows
        // the stopped state, from which it can be started) but reads dimmed
        // like every other stopped app.
        const stoppedFilesApp =
          row.tabType === "files" && !isDisabledFilesRow
            ? (getApps().find((app) => app.name === "files" && !isAppRunning(app)) ?? null)
            : null;
        // The row's label is derived from its mode -- "New Chat" while the
        // shortcut always creates, "Chat" while it focuses -- and nothing else
        // about the row changes with it.
        const mode = shortcutModeForProject(project, row.tabType);
        const label = mode === "new" && !isDisabledFilesRow ? `New ${row.label}` : row.label;
        // While a create this shortcut asked for is pending, the row stands
        // down so a second click cannot start a second object.
        const isAwaitingCreate = attrs.awaitingShortcutIds.has(row.tabType);
        return shortcutRow({
          key: `tab-type:${row.tabType}`,
          iconMarkup: railIcon(row.tabType, ROW_ICON_SIZE),
          label: isAwaitingCreate ? "Starting…" : label,
          baseLabel: row.label,
          tooltip: isDisabledFilesRow
            ? FILE_VIEWER_TOOLTIP
            : stoppedFilesApp !== null
              ? `${label} — ${appStoppedDetail(stoppedFilesApp)}`
              : SHORTCUT_TOOLTIPS[row.tabType],
          isDimmed: stoppedFilesApp !== null,
          onclick: isDisabledFilesRow || isAwaitingCreate ? null : () => pick(() => attrs.onOpenTabType(row.tabType)),
          onUnpin: canUnpin ? () => attrs.onSetShortcutPinned(row.tabType, false) : null,
          onMenu: (event: MouseEvent) =>
            openMenuAt({ kind: "shortcut", anchor: anchorForEvent(event), shortcutId: row.tabType }),
          isMenuOpen: openMenu?.kind === "shortcut" && openMenu.shortcutId === row.tabType,
        });
      }),
      ...shortcutApps.map((app) => pinnedAppRow(app, attrs)),
    ]);
  }

  function allAppsRow(): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        // Quieter than the shortcuts above it: this is the way to the rest of
        // the machine, not one of the machine's own starting points.
        class: `project-rail-all-apps ${ROW_CLASS} text-faint hover:bg-fill-hover hover:text-secondary`,
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
      [m("span", { class: ICON_BOX_CLASS }, m.trust(railIcon("ellipsis", ROW_ICON_SIZE))), railLabel("All apps", "")],
    );
  }

  function searchPill(viewName: string): m.Vnode {
    return m("div", { class: "my-1 flex h-7 shrink-0 items-center gap-2 rounded-md bg-sidebar px-2 text-faint" }, [
      m("span", { class: "flex shrink-0 items-center" }, m.trust(railIcon("search", ACTION_ICON_SIZE))),
      m("input", {
        type: "text",
        class:
          `project-rail-search min-w-0 flex-1 bg-transparent ${ROW_TEXT_CLASS} text-primary outline-none ` +
          "placeholder:text-faint",
        placeholder: `Find a tab in ${viewName}`,
        value: searchQuery,
        oninput: (event: InputEvent) => {
          searchQuery = (event.target as HTMLInputElement).value;
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Escape") searchQuery = "";
        },
      }),
    ]);
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

  /** A row mid-rename: its icon stays put and its label becomes a live text
   *  field seeded with the row's current name. Committing happens on blur --
   *  matching the tab's own inline editor, and for the same reason: it is how
   *  the edit ends on every path out (Enter, clicking another row, the rail
   *  itself collapsing) without each needing its own handler. Escape instead
   *  discards it; the `renamingRef` guard on `onblur` keeps that discard from
   *  being immediately overwritten by the blur its own removal triggers. */
  function renameRow(row: SidebarTabRow, attrs: SidebarAttrs): m.Vnode {
    return m("div", { key: row.ref, class: `${ROW_CLASS} pr-1` }, [
      m("span", { class: ICON_BOX_CLASS }, m.trust(rowIconMarkup(row))),
      m("input", {
        type: "text",
        class:
          `min-w-0 flex-1 rounded border border-default bg-sidebar px-1 ${ROW_TEXT_CLASS} ` +
          "text-primary outline-none",
        value: renameDraft,
        oncreate: (vnode: m.VnodeDOM) => {
          const input = vnode.dom as HTMLInputElement;
          input.focus();
          input.select();
        },
        onclick: (event: MouseEvent) => event.stopPropagation(),
        oninput: (event: InputEvent) => {
          renameDraft = (event.target as HTMLInputElement).value;
        },
        onblur: () => {
          if (renamingRef !== row.ref) return;
          commitRename(row, renameDraft, attrs);
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter") (event.target as HTMLInputElement).blur();
          else if (event.key === "Escape") endRename();
        },
      }),
    ]);
  }

  function tabRow(row: SidebarTabRow, ranges: readonly MatchRange[], attrs: SidebarAttrs, hasMenu: boolean): m.Vnode {
    if (renamingRef === row.ref) return renameRow(row, attrs);
    const isMenuOpenHere = openMenu?.kind === "row" && openMenu.ref === row.ref;
    return m(
      "div",
      {
        key: row.ref,
        // No persistent selected-row highlight: the dock shows several panes at
        // once, so marking one row as "the" selection would misread. Hover is
        // the only fill.
        class:
          `project-rail-tab group ${ROW_CLASS} pr-1 hover:bg-fill-hover ` +
          (row.stoppedDetail !== undefined
            ? "project-rail-tab-stopped text-faint opacity-60"
            : row.isOpen
              ? "text-primary"
              : "text-faint"),
        ...(row.stoppedDetail === undefined ? {} : hoverTooltipAttrs(`${row.label} — ${row.stoppedDetail}`, "right")),
        onclick: () =>
          pick(() => {
            attrs.onOpenRow(row);
            // Already open: focusing its tab changed nothing the user can see
            // through an expanded rail sitting on top of it, so the click
            // would otherwise look like it did nothing. Force the rail closed
            // the same way a completed view switch does (see
            // `lastRenderedViewId` below) rather than waiting on a mouseleave
            // that may not come until well after the click.
            if (row.isOpen) expanded = false;
          }),
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          if (!hasMenu) return;
          openMenuAt({ kind: "row", anchor: anchorForPointer(event), ref: row.ref });
        },
      },
      [
        m("span", { class: ICON_BOX_CLASS }, m.trust(rowIconMarkup(row))),
        m(
          "span",
          { class: `min-w-0 flex-1 truncate ${ROW_TEXT_CLASS} whitespace-nowrap` },
          matchedLabel(row.label, ranges),
        ),
        // The one-click remove survives only on a row with no menu (a legacy
        // "url" member, which would otherwise be unfilable from nowhere -- see
        // objectMenuKindForRow). Every other kind's kebab menu carries "Remove
        // from project", and a bare hover control this close to the row was
        // too easy to hit by accident. Absent in Everything, which is the
        // home: an object leaves it only by being destroyed.
        isEverythingView(attrs.activeViewId) || hasMenu
          ? null
          : railAction({
              iconMarkup: railIcon("remove-from-view", ACTION_ICON_SIZE),
              label: `Remove ${row.label} from this project`,
              tooltip:
                "Removes it from this project only. It keeps running, and stays in Everything and in any " +
                "other project showing it.",
              tooltipPlacement: "right",
              extra: "project-rail-remove",
              onclick: () => attrs.onRemoveFromView(row),
            }),
        hasMenu
          ? railAction({
              iconMarkup: railIcon("kebab", ACTION_ICON_SIZE),
              label: `Actions for ${row.label}`,
              isRevealed: isMenuOpenHere,
              onclick: (event) => {
                if (isMenuOpenHere) {
                  openMenu = null;
                  return;
                }
                openMenuAt({ kind: "row", anchor: anchorForEvent(event), ref: row.ref });
              },
            })
          : null,
      ],
    );
  }

  function tabList(attrs: SidebarAttrs, shortcutApps: readonly AppEntry[]): m.Vnode {
    // An app that HAS a shortcut row above is shown once, there. Listing it
    // here too put the same app in the rail twice, with two of everything --
    // two names to keep in step, and two controls for the one act of taking it
    // out of the project.
    //
    // Only the ones actually drawn above are dropped, which is the whole reason
    // this takes `shortcutApps` rather than testing the kind: a member of an
    // app the machine no longer offers has no icon or URL to draw a shortcut
    // from, so it is not up there, and dropping it here as well would leave it
    // showing nowhere and removable from nowhere.
    //
    // The rows are still read UNFILTERED elsewhere -- `pinnedAppNamesForView`
    // builds the shortcut strip out of exactly these app rows -- so this drops
    // them from the list and from nothing else.
    const shownAbove = new Set(shortcutApps.map((app) => memberRef("app", app.name)));
    const listedRows = attrs.rows.filter((row) => !shownAbove.has(row.ref));
    const results = searchMembers(listedRows, searchQuery);
    if (results.length === 0) {
      return m(
        "div",
        { class: `px-2 py-2 ${ROW_TEXT_CLASS} text-faint` },
        listedRows.length === 0 ? "Nothing here yet." : "No tabs match that.",
      );
    }
    return m(
      "div",
      { class: "min-h-0 flex-1 overflow-x-hidden overflow-y-auto" },
      results.map((result) => {
        const row = result.member;
        return tabRow(row, result.labelRanges, attrs, objectMenuKindForRow(row) !== null);
      }),
    );
  }

  // ---------- Floating menus ----------

  /** The transparent overlay behind any open menu -- see MENU_SCRIM_CLASS. Its
   *  own pointerdown closes the open menu and stops there: `stopPropagation`
   *  keeps the press from also reaching (and activating) whatever rail or
   *  dock control it visually sits on top of, which is the whole reason it
   *  exists rather than relying on `handleOutsideMousedown` alone. */
  function menuScrim(): m.Vnode {
    return m("div", {
      class: MENU_SCRIM_CLASS,
      onmousedown: (event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        closeMenus();
      },
    });
  }

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
    isDestructive?: boolean;
    // Tertiary at rest ("New project" is the one user today), text-faint
    // -- but reads as clickable rather than disabled, so it goes fully
    // primary on hover rather than staying faint.
    isQuiet?: boolean;
    // Rendered faint and inert: the verb exists but cannot act right now
    // ("Focus last X" while the view shows no X). Distinct from isQuiet,
    // which is clickable.
    isDisabled?: boolean;
    tooltip?: string | null;
    onclick: (event: MouseEvent) => void;
    onmouseenter?: (event: MouseEvent) => void;
    trailing?: m.Children;
    // Overrides the row's own chrome and its icon's leading box, so the
    // switcher's project rows can borrow the rail's own tighter geometry
    // (SWITCHER_ROW_CLASS + ICON_BOX_CLASS) instead of a menu's roomier
    // default -- see switcherMenu, the one caller that needs its icons and
    // labels to land at the same x the rail itself draws them at.
    rowClass?: string;
    iconBoxClass?: string;
  }): m.Vnode {
    const tone = options.isDisabled
      ? "project-rail-menu-item-disabled text-faint cursor-default hover:bg-transparent"
      : options.isDestructive
        ? "text-red-600"
        : options.isQuiet
          ? "text-faint hover:text-primary"
          : "text-primary";
    return m(
      "div",
      {
        class: `${options.rowClass ?? MENU_ROW_CLASS} ${tone}`,
        role: "menuitem",
        "aria-disabled": options.isDisabled === true ? "true" : undefined,
        ...(options.tooltip === null || options.tooltip === undefined ? {} : hoverTooltipAttrs(options.tooltip)),
        onclick: options.isDisabled === true ? undefined : options.onclick,
        ...(options.onmouseenter === undefined ? {} : { onmouseenter: options.onmouseenter }),
      },
      [
        options.iconMarkup === null
          ? null
          : m(
              "span",
              { class: options.iconBoxClass ?? "flex w-4 shrink-0 items-center justify-center" },
              m.trust(options.iconMarkup),
            ),
        m("span", { class: "min-w-0 flex-1 truncate" }, options.label),
        options.trailing ?? null,
      ],
    );
  }

  /** The pencil a switcher row carries: opens that row's own project
   *  settings, never the row it happens to render in the current view. It
   *  stops propagation so it never also fires the row's own click (switch to
   *  that project, or nothing on the one already mounted) -- the same shape
   *  the tab list's kebab uses for the same reason. `isStacked` sizes it to
   *  fill its wrapper exactly, so it can sit under the active row's checkmark
   *  and swap places with it on hover instead of beside it (see
   *  `switcherRowTrailing`); an inactive row's pencil is the only thing in its
   *  own slot and needs no such positioning. */
  function switcherEditButton(
    project: ProjectInfo,
    onOpen: (project: ProjectInfo) => void,
    isStacked: boolean,
  ): m.Vnode {
    return railAction({
      iconMarkup: icon("edit", { size: ACTION_ICON_SIZE, strokeWidth: 1.75 }),
      label: `Edit ${project.name}`,
      tooltip: `Edit ${project.name}`,
      extra: isStacked ? "absolute inset-0" : "",
      onclick: () => onOpen(project),
    });
  }

  /**
   * A switcher row's trailing control.
   *
   * The active view's own row carries a checkmark instead of the plain
   * background fill the switcher used to mark it with -- and, for a project
   * (not Everything), that checkmark swaps for the same rename pencil every
   * other row reveals on hover, rather than showing both at once. That still
   * leaves every project renameable exactly one way: hover its row for the
   * pencil, whether or not it happens to be the active one.
   *
   * `onOpen` is null for Everything, which is not a project and has no
   * settings to open -- its active row is a bare, unswapped checkmark.
   */
  function switcherRowTrailing(
    isActive: boolean,
    project: ProjectInfo | null,
    onOpen: ((project: ProjectInfo) => void) | null,
  ): m.Vnode | null {
    if (project === null || onOpen === null) {
      return isActive
        ? m(
            "span",
            {
              class: "project-rail-check flex h-5 w-5 shrink-0 items-center justify-center text-secondary",
            },
            m.trust(icon("check", { size: ACTION_ICON_SIZE })),
          )
        : null;
    }
    if (!isActive) return switcherEditButton(project, onOpen, false);
    return m("span", { class: "relative flex h-5 w-5 shrink-0 items-center justify-center" }, [
      m(
        "span",
        {
          class:
            "project-rail-check pointer-events-none absolute inset-0 flex items-center justify-center " +
            "text-secondary transition-opacity duration-100 group-hover:opacity-0",
        },
        m.trust(icon("check", { size: ACTION_ICON_SIZE })),
      ),
      switcherEditButton(project, onOpen, true),
    ]);
  }

  function switcherMenu(attrs: SidebarAttrs, anchor: MenuAnchor): m.Vnode {
    const isEverythingActive = isEverythingView(attrs.activeViewId);
    return floatingCard({
      anchor,
      placement: "below",
      role: "menu",
      // A touch wider than the rail it hangs off (see SWITCHER_MENU_WIDTH),
      // not the header's own width -- a project name plus its trailing
      // control need a little more room than that.
      width: SWITCHER_MENU_WIDTH,
      children: [
        attrs.projects.map((project) => {
          const isCurrent = project.project_id === attrs.activeViewId;
          return menuRow({
            iconMarkup: viewIdentityMarkup(project, ROW_ICON_SIZE),
            label: project.name,
            // The rail's own row geometry, not a generic menu row's: it puts
            // a project's icon and label at the same x the rail draws its
            // own rows at, so the switcher reads as a continuation of the
            // rail underneath it rather than an oddly-padded dropdown.
            rowClass: SWITCHER_ROW_CLASS,
            iconBoxClass: ICON_BOX_CLASS,
            trailing: switcherRowTrailing(isCurrent, project, (target) =>
              pick(() => {
                settingsProject = target;
              }),
            ),
            onclick: () =>
              pick(() => {
                // Already there: the row's click target is spent, and its
                // pencil (not this) is what opens its settings now.
                if (isCurrent) return;
                attrs.onSelectView(project.project_id);
              }),
          });
        }),
        menuRow({
          iconMarkup: railIcon("plus", ROW_ICON_SIZE),
          label: "New project",
          isQuiet: true,
          rowClass: SWITCHER_ROW_CLASS,
          iconBoxClass: ICON_BOX_CLASS,
          onclick: () => {
            void createNewProject(attrs);
          },
        }),
        menuError === null ? null : m("div", { class: "px-3 py-1 text-[12px] text-red-600" }, menuError),
        m("div", { class: "my-1 border-t border-default" }),
        // Everything sits below the divider because it is not one of the
        // projects -- it is the unfiltered view they all live inside -- but it
        // is picked exactly like one, and has a dock of its own.
        menuRow({
          iconMarkup: compositeSquiggleMarkup(ROW_ICON_SIZE),
          label: EVERYTHING_VIEW_NAME,
          rowClass: SWITCHER_ROW_CLASS,
          iconBoxClass: ICON_BOX_CLASS,
          trailing: switcherRowTrailing(isEverythingActive, null, null),
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

  /**
   * A row's kebab/context menu: the same shared verb set the tab's own ⋮ menu
   * renders (see objectMenu.ts), so right-clicking a rail row and opening the
   * tab it stands for offer exactly the same list. ``railMenuActions`` is the
   * only place that differs from the tab's build -- everything about WHICH
   * verbs show, in what order, is fixed by ``objectMenuEntries`` alone.
   */
  function rowMenu(attrs: SidebarAttrs, menu: Extract<OpenMenu, { kind: "row" }>): m.Children {
    const row = attrs.rows.find((candidate) => candidate.ref === menu.ref);
    if (row === undefined) return null;
    const kind = objectMenuKindForRow(row);
    if (kind === null) return null;
    const entries = objectMenuEntries(kind, railMenuActions(row, attrs));
    // A pinned app's one row is both an object and a shortcut, so its one menu
    // carries both: the object verbs as they are, a divider, then the shortcut
    // group (complementary action, mode flip). No unpin entry here -- the
    // app's "Remove from project" IS its unpin.
    const shortcutEntries =
      menu.shortcutAppName === undefined
        ? []
        : shortcutMenuEntries(appShortcutId(menu.shortcutAppName), row.label, attrs);
    return floatingCard({
      anchor: menu.anchor,
      placement: "right",
      role: "menu",
      width: null,
      children: [
        ...entries.map((entry) =>
          entry === OBJECT_MENU_DIVIDER
            ? m("div", { class: "my-1 border-t border-default" })
            : menuRow({
                iconMarkup: icon(entry.iconName, { size: ACTION_ICON_SIZE }),
                label: entry.label,
                isDestructive: entry.isDestructive,
                onclick: () => pick(entry.run),
              }),
        ),
        ...(shortcutEntries.length === 0 ? [] : [m("div", { class: "my-1 border-t border-default" })]),
        ...shortcutEntries.map((entry) =>
          menuRow({
            iconMarkup: null,
            label: entry.label,
            isDisabled: entry.isDisabled,
            onclick: () => pick(entry.run),
          }),
        ),
      ],
    });
  }

  function allAppsMenu(
    attrs: SidebarAttrs,
    anchor: MenuAnchor,
    projectName: string | null,
    pinnedAppNames: string[],
  ): m.Vnode {
    return floatingCard({
      anchor,
      placement: "right",
      role: "dialog",
      width: null,
      children: m(AllAppsPicker, {
        projectName,
        pinnedAppNames,
        // The rail owns what a built-in row is called and what it looks like,
        // so it hands those over resolved rather than the popover learning a
        // second copy of the vocabulary.
        unpinnedShortcuts: unpinnedShortcutRows(attrs),
        onOpenApp: (app: AppEntry) => pick(() => attrs.onOpenApp(app)),
        onOpenShortcut: (shortcut) => pick(() => attrs.onOpenTabType(shortcut)),
        onPinShortcut: (shortcut) => {
          // Pinning is not picking, as with an app: the popover stays open so
          // several rows can be put back in one visit.
          attrs.onSetShortcutPinned(shortcut, true);
        },
        onTogglePin: (app: AppEntry, wanted: boolean) => {
          // Pinning is not picking: the popover stays open so several apps can
          // be pinned in one visit.
          attrs.onSetAppPinned(app, wanted);
        },
      }),
    });
  }

  /** The settings modal, opened from the header's context menu or from any
   *  switcher row's edit pencil. It re-lists on the way back out rather than
   *  patching the cached registry: the server normalizes the name, and a
   *  delete has to be reconciled against the mounted view anyway -- which the
   *  workspace does off the `project_deleted` broadcast, the same path
   *  another client's delete takes. */
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
    });
  }

  return {
    view(vnode) {
      const attrs = vnode.attrs;
      if (lastRenderedViewId !== null && lastRenderedViewId !== attrs.activeViewId) {
        closeMenus();
        // The row being renamed does not survive the switch either: it is not
        // even necessarily still in the destination view's own row list.
        endRename();
      }
      lastRenderedViewId = attrs.activeViewId;
      // A rename also ends when its row simply stops being listed: the object
      // behind it can be destroyed from another surface (or drop off a fleet
      // refresh) while the field is up, and removing a focused input fires no
      // blur, so the field's own exit never runs. Left set, `renamingRef`
      // holds the rail open against every future mouseleave -- and refs are
      // handed out again (the terminal allocator reuses `terminal-N`), so the
      // next row to answer to this one would come back wearing the abandoned
      // draft.
      if (renamingRef !== null && !attrs.rows.some((row) => row.ref === renamingRef)) endRename();
      const isEverything = isEverythingView(attrs.activeViewId);
      const project = attrs.projects.find((candidate) => candidate.project_id === attrs.activeViewId) ?? null;
      const viewName = isEverything ? EVERYTHING_VIEW_NAME : (project?.name ?? "");

      // The app list is whatever AgentManager last heard over the WebSocket.
      // Every handled WS event ends in an `m.redraw()`, so an `apps_updated`
      // push repaints the rail without a subscription of its own.
      const machineApps = pickableApps();
      // The view's pinned apps, which is to say its app members. A name the
      // machine no longer offers -- a member of an app that has since been
      // unregistered -- has no icon or URL to draw a shortcut from, so it drops
      // out here and stays in the tab list, where it can still be removed.
      const pinnedAppNames = pinnedAppNamesForView(attrs.rows, isEverything);
      // Everything shows a fixed shortcut row for every openable app (built-in
      // rows first in rail order, then apps alphabetically -- pickableApps'
      // own order): it is the home, a newly added app appears here without
      // anyone pinning anything, and there is no registry entry to record an
      // unpin against, so the rows are fixed.
      const shortcutApps = isEverything
        ? machineApps
        : pinnedAppNames
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
            document.addEventListener("mouseout", handlePointerLeftWindow);
            document.documentElement.addEventListener("mouseleave", handleDocumentPointerLeave);
            window.addEventListener("blur", handleWindowBlur);
          },
          onremove: () => {
            rootElement = null;
            document.removeEventListener("mousedown", handleOutsideMousedown);
            document.removeEventListener("keydown", handleKeydown);
            document.removeEventListener("mouseout", handlePointerLeftWindow);
            document.documentElement.removeEventListener("mouseleave", handleDocumentPointerLeave);
            window.removeEventListener("blur", handleWindowBlur);
          },
          onmouseenter: () => {
            isPointerOverRail = true;
            expanded = true;
          },
          onmouseleave: () => {
            // The rail follows the pointer: leaving it folds it back up, and a
            // menu that was open goes with it rather than being left hanging
            // over the dock with nothing behind it.
            //
            // An open menu is the exception: it holds the rail open while the
            // pointer works down it, since every one of them extends past the
            // rail's own box (some further than others, "All apps" most of
            // all) -- exactly the case a plain mouseleave would get wrong,
            // folding the rail out from under a menu the pointer is still
            // using the moment it crosses that edge to reach it. A row mid-
            // rename holds the rail open the same way: its input sits inside
            // the rail's own card, and collapsing out from under a half-typed
            // name would commit whatever was typed so far (native blur-on-
            // removal) without the user having asked to.
            //
            // Recorded before either exception takes it, since this is the
            // only leave the browser sends for this crossing: an edit that
            // ends after it has to know the pointer is gone to fold the rail
            // up in its place (see `endRename`).
            isPointerOverRail = false;
            if (openMenu !== null || renamingRef !== null) return;
            closeMenus();
          },
        },
        [
          m(
            "div",
            {
              class:
                // `bottom-[4px]` rather than `inset-y-0`: expanded, this is a
                // floating card, and it needs the same gap under it that the
                // canvas gives it above and to its left -- otherwise its
                // rounded bottom corners and shadow run off the window edge.
                // The dock itself stays flush at the bottom; this insets only
                // the rail.
                // `select-none`: the expanded rail overlays the panels to its
                // right, so a drag starting on a row must not select the
                // labels it sweeps across.
                "machine-sidebar absolute top-0 bottom-[4px] left-0 z-20 flex flex-col overflow-hidden border select-none " +
                `${RAIL_PADDING_CLASS} transition-[width] duration-150 ease-out ` +
                (expanded ? EXPANDED_CLASS : COLLAPSED_CLASS),
            },
            [
              header(project, viewName),
              // Always rendered, unlike the rest of the expanded-only chrome
              // below: it sits between rows both rail states share (the
              // header and the shortcuts), so removing it collapsed rather
              // than just hiding its line would shift those shared rows down
              // by its height the instant the rail expands.
              m("div", { class: `${DIVIDER_CLASS} mb-1 ` + (expanded ? "border-default" : "border-transparent") }),
              shortcuts(attrs, shortcutApps),
              expanded ? allAppsRow() : null,
              expanded ? m("div", { class: `${DIVIDER_CLASS} mt-1` }) : null,
              expanded ? searchPill(viewName) : null,
              expanded ? tabList(attrs, shortcutApps) : null,
            ],
          ),
          openMenu === null ? null : menuScrim(),
          openMenu?.kind === "switcher" ? switcherMenu(attrs, openMenu.anchor) : null,
          openMenu?.kind === "header" && project !== null ? headerMenu(project, openMenu.anchor) : null,
          openMenu?.kind === "allApps"
            ? allAppsMenu(attrs, openMenu.anchor, isEverything ? null : viewName, pinnedAppNames)
            : null,
          openMenu?.kind === "row" ? rowMenu(attrs, openMenu) : null,
          openMenu?.kind === "shortcut" ? shortcutMenu(attrs, openMenu) : null,
          settingsModal(attrs),
        ],
      );
    },
  };
}
