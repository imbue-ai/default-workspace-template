/**
 * The project rail: the narrow strip down the left edge of the workspace that says which view
 * you are in and lists everything that view holds.
 *
 * At rest it is a 37px icon strip showing only the view's glyph and the shortcut icons; the
 * pointer entering expands it to a 240px panel that floats over the dock. The labels are always
 * in the DOM and only transition their opacity, so the expansion is one width/opacity
 * animation rather than a reflow. The rail is absolutely positioned inside a 37px slot for the
 * same reason: expanding overlays the dock instead of shoving it sideways.
 *
 * A **view** is either a project (a shared tab set plus its own arrangement) or Everything
 * (every instance on the machine, and the home). The rail draws both identically; Everything
 * has no settings, a fixed rail of every app's primary action, and nothing can be removed
 * from it.
 *
 * The rail opens nothing itself. The workspace owns panel creation, filing and every verb, so
 * each row calls back with what the user picked (see SidebarAttrs). Nothing here knows what
 * any app is: a row's icon, name and status are the inventory's.
 */

import m from "mithril";
import { findInstance, getApp, getOpenableApps, isAppStoppable, primaryActionForApp } from "../models/Inventory";
import { normalizeTabTitle } from "./tab-rename";
import type {
  AppAction,
  AppRecord,
  InstanceStatus,
  ProjectInfo,
  ProjectShortcut,
  ShortcutMode,
} from "../models/Inventory";
import {
  isEverythingView,
  projectForViewId,
  searchRows,
  EVERYTHING_VIEW_ID,
  EVERYTHING_VIEW_NAME,
} from "../models/Projects";
import { createProject } from "../models/Projects";
import type { MatchRange } from "../models/Projects";
import { AllAppsPicker } from "./AllAppsPicker";
import { appIconMarkup } from "./components/appIcon";
import { Button, buttonClass } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { TooltipPlacement } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import { createMenu } from "@imbue/workspace-ui/src/components/menu";
import type { Menu, MenuRow } from "@imbue/workspace-ui/src/components/menu";
import type { MenuAnchor } from "@imbue/workspace-ui/src/menu-position";
import { TAB_MENU_DIVIDER, tabMenuEntries } from "./tabMenu";
import type { TabMenuActions } from "./tabMenu";
import { ProjectSettingsModal } from "./ProjectSettingsModal";
import { SQUIGGLE_GLYPHS, compositeSquiggleMarkup, monogramMarkup, squiggleMarkup } from "./squiggles";

/**
 * One row of the rail's tab list: an instance the active view holds, whether or not it
 * currently has a tab. The workspace builds these from the inventory; the rail only renders,
 * filters and offers actions on them.
 */
export interface SidebarTabRow {
  address: string;
  appName: string;
  appDisplayName: string;
  // The instance's key within its app ("" for a single-instance app's one record).
  instanceKey: string;
  label: string;
  // Whether the instance has a tab in the dock right now. Open rows read as primary text,
  // undocked ones (listed, with no tab) as tertiary.
  isOpen: boolean;
  status: InstanceStatus;
  renameable: boolean;
  stoppable: boolean;
  // Why this row's app is not running, when it is not. A stopped row renders dimmed with
  // this as its tooltip.
  stoppedDetail?: string;
}

/** One rail shortcut, resolved against the inventory: the app and the action it runs. */
export interface ResolvedShortcut {
  app: AppRecord;
  action: AppAction;
  mode: ShortcutMode;
  shortcut: ProjectShortcut;
}

export interface SidebarAttrs {
  projects: readonly ProjectInfo[];
  activeViewId: string;
  rows: readonly SidebarTabRow[];
  onSelectView: (viewId: string) => void;
  onProjectsChanged: () => void;
  onProjectCreated: (projectId: string) => void;
  // Run one rail shortcut in its mode.
  onRunShortcut: (shortcut: ProjectShortcut) => void;
  // Always run the shortcut's action -- the menu's complementary action while the row focuses.
  onRunShortcutAsNew: (shortcut: ProjectShortcut) => void;
  // Focus the most recently used instance of the shortcut's app -- the complementary action
  // while the row creates. Only called while the active view shows one.
  onFocusLastOfShortcut: (shortcut: ProjectShortcut) => void;
  // Flip one shortcut's mode for this project. Never called under Everything.
  onSetShortcutMode: (shortcut: ProjectShortcut, mode: ShortcutMode) => void;
  // Take one shortcut off this project's rail. Never called under Everything.
  onRemoveShortcut: (shortcut: ProjectShortcut) => void;
  // Add an app's action to this project's rail (the All apps popover's pin).
  onPinShortcut: (app: AppRecord, action: AppAction) => void;
  // Run an app's action from the All apps popover: always creates.
  onRunAppAction: (app: AppRecord, action: AppAction) => void;
  // ``<app>:<action>`` keys whose create is in flight right now: those rows stand down.
  awaitingActionKeys: ReadonlySet<string>;
  onOpenRow: (row: SidebarTabRow) => void;
  onRefreshRow: (row: SidebarTabRow) => void;
  onRenameRow: (row: SidebarTabRow, title: string) => void;
  onShareApp: (appName: string) => void;
  onAddRowToProjects: (row: SidebarTabRow) => void;
  onRemoveFromView: (row: SidebarTabRow) => void;
  onAppLifecycle: (appName: string, action: "stop" | "start") => void;
  onInstanceLifecycle: (row: SidebarTabRow, action: "stop" | "start") => void;
  onDeleteRow: (row: SidebarTabRow) => void;
}

const COLLAPSED_CLASS = "w-[37px] border-transparent bg-transparent";
const EXPANDED_CLASS = "w-[240px] rounded-lg border-default bg-surface shadow-overlay";

const RAIL_PADDING_CLASS = "p-[5px]";
const ICON_BOX_CLASS = "flex w-[20px] shrink-0 items-center justify-center";
const DIVIDER_CLASS = "-mx-[5px] shrink-0 border-t border-default";
const ROW_TEXT_CLASS = "text-(length:--font-size-row)";
const ROW_ICON_SIZE = 16;
const ACTION_ICON_SIZE = 14;
const ROW_CLASS = "flex h-7 w-full shrink-0 cursor-pointer items-center gap-1 rounded-md text-left";

/** The rail's menus are the shared Menu; `project-rail-menu` is a bare marker for tests, and the
 *  rail's own text size rides along so a menu reads at the size of the rows beside it. */
const MENU_MARKER_CLASS = `project-rail-menu ${ROW_TEXT_CLASS} text-primary`;
/** Every menu row: a bare marker, plus `group` for the trailing controls that reveal on the
 *  row's hover. tightGap (4px) matches the rail's own rows (ROW_CLASS), so a menu row reads as
 *  tight as the rail row sitting right above it. */
const MENU_ROW_EXTRA = "project-rail-menu-item group";
const SWITCHER_MENU_WIDTH = 256;

const RAIL_PATHS = {
  app: '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  kebab:
    '<circle cx="12" cy="5" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/>',
  ellipsis:
    '<circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/>',
  pin:
    '<path d="M9 4h6l-1 5 3 3v2H7v-2l3-3-1-5z" fill="currentColor" stroke="currentColor"/>' +
    '<line x1="12" y1="14" x2="12" y2="20" fill="none" stroke="currentColor"/>',
} as const;

type RailIconName = keyof typeof RAIL_PATHS;

const XMLNS = "http://www.w3.org/2000/svg";

function railIcon(name: RailIconName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${RAIL_PATHS[name]}</svg>`
  );
}

/** The glyph an app wears everywhere in the rail: its own icon, or its monogram. */
function appGlyph(app: AppRecord | undefined, size: number): string {
  const fallback = railIcon("app", size);
  return app === undefined ? fallback : appIconMarkup(app.icon, size, fallback, app.name);
}

/** Full <svg> string for a view's identity. */
function viewIdentityMarkup(project: ProjectInfo | null, size: number): string {
  if (project === null) return compositeSquiggleMarkup(size);
  const isDrawable = Number.isInteger(project.glyph) && project.glyph >= 0 && project.glyph < SQUIGGLE_GLYPHS.length;
  return isDrawable
    ? squiggleMarkup(project.glyph, project.color || null, size)
    : monogramMarkup(project.name, project.color, size);
}

// ---------- Floating menu anchors ----------

function anchorForEvent(event: Event): MenuAnchor {
  return (event.currentTarget as HTMLElement).getBoundingClientRect();
}

function anchorForPointer(event: MouseEvent): MenuAnchor {
  return { left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0 };
}

/** One hover-revealed row action: every trailing micro-control on a rail or menu row (unpin,
 *  kebab, remove-from-project) is this single recipe -- the shared Button at its xs icon size,
 *  ghost variant, hidden until the row's `group` hover reveals it (or held visible while its
 *  own menu is open), and always stopping propagation so the row underneath never also fires.
 *  Positioning and marker classes ride `extra`. */
function railAction(options: {
  iconMarkup: string;
  label: string;
  tooltip?: string;
  tooltipPlacement?: TooltipPlacement;
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

// ---------- Shortcuts ----------

/**
 * The rail a view shows, resolved against the inventory.
 *
 * A project's rail is its stored shortcut list, in rail order, dropping any whose app or
 * action the machine no longer offers. Everything's rail is fixed: every openable app's primary
 * action, in registry order, in focus mode -- it is the home, and a newly registered app
 * appears there without anyone pinning anything.
 */
export function effectiveShortcuts(project: ProjectInfo | null, apps: readonly AppRecord[]): ResolvedShortcut[] {
  if (project === null) {
    return apps.flatMap((app) => {
      const action = primaryActionForApp(app);
      if (action === null) return [];
      return [{ app, action, mode: "focus" as const, shortcut: { app: app.name, action: action.id, mode: "focus" } }];
    });
  }
  return project.shortcuts.flatMap((shortcut) => {
    const app = apps.find((candidate) => candidate.name === shortcut.app);
    const action = app?.actions.find((candidate) => candidate.id === shortcut.action);
    if (app === undefined || action === undefined) return [];
    return [{ app, action, mode: shortcut.mode, shortcut }];
  });
}

/** What a shortcut row reads: the action's label while it always creates ("New Terminal"), the
 *  app's name while it focuses ("Terminal"). */
export function shortcutLabel(resolved: ResolvedShortcut): string {
  return resolved.mode === "new" ? resolved.action.label : resolved.app.display_name;
}

// ---------- New projects ----------

/** The name a fresh project gets: the first "Project N" nobody is using, by name or by id. */
export function nextProjectName(projects: readonly Pick<ProjectInfo, "name" | "id">[]): string {
  const takenNames = new Set(projects.map((project) => project.name.trim().toLowerCase()));
  const takenIds = new Set(projects.map((project) => project.id));
  let index = 1;
  while (takenNames.has(`project ${index}`) || takenIds.has(`project-${index}`)) index += 1;
  return `Project ${index}`;
}

/** The glyph a fresh project gets: the first unused squiggle, then repeating. */
export function nextGlyphIndex(usedGlyphs: readonly number[]): number {
  const used = new Set(usedGlyphs);
  for (let index = 0; index < SQUIGGLE_GLYPHS.length; index += 1) {
    if (!used.has(index)) return index;
  }
  return usedGlyphs.length % SQUIGGLE_GLYPHS.length;
}

// ---------- The component ----------

function shortcutKey(shortcut: ProjectShortcut): string {
  return `${shortcut.app}:${shortcut.action}`;
}

export function Sidebar(): m.Component<SidebarAttrs> {
  let expanded = false;
  let settingsProject: ProjectInfo | null = null;
  let renamingAddress: string | null = null;
  let renameDraft = "";
  let isPointerOverRail = false;
  let searchQuery = "";
  let menuError: string | null = null;
  let lastRenderedViewId: string | null = null;

  // Which row, and which shortcut, the row and shortcut menus are open for. The menus
  // themselves are below; these ride beside them because the shared menu knows nothing about
  // rails.
  let menuRowAddress: string | null = null;
  let menuShortcutKey: string | null = null;

  /** One of the rail's menus: the shared Menu with the rail's marker class, and the rail's
   *  expansion tied to it -- a menu closing takes the expansion with it unless the pointer is
   *  still on the rail, since the menu floats beside the rail and the pointer is usually off
   *  it by the time the menu closes. */
  function railMenu(placement: "below" | "right", width: number | null, role: "menu" | "dialog" = "menu"): Menu {
    return createMenu({
      placement,
      ...(width === null ? {} : { width }),
      role,
      extraClass: MENU_MARKER_CLASS,
      onClose: () => {
        menuError = null;
        if (!isPointerOverRail) expanded = false;
      },
    });
  }

  const switcherMenu = railMenu("below", SWITCHER_MENU_WIDTH);
  const headerMenu = railMenu("right", null);
  const allAppsMenu = railMenu("right", null, "dialog");
  const rowMenu = railMenu("right", null);
  const shortcutMenu = railMenu("right", null);
  const railMenus = [switcherMenu, headerMenu, allAppsMenu, rowMenu, shortcutMenu];

  function isRailMenuOpen(): boolean {
    return railMenus.some((menu) => menu.isOpen());
  }

  function isAnyMenuOpen(): boolean {
    return isRailMenuOpen() || settingsProject !== null;
  }

  function closeMenus(): void {
    for (const menu of railMenus) menu.close();
    menuError = null;
    expanded = false;
  }

  /** Collapse on the window losing focus, which is how a click into a cross-origin pane is
   *  seen. A menu stays: it closes on a press or Escape and on nothing else. */
  function handleWindowBlur(): void {
    isPointerOverRail = false;
    if (renamingAddress !== null || isAnyMenuOpen() || !expanded) return;
    expanded = false;
    m.redraw();
  }

  function handleDocumentPointerLeave(): void {
    isPointerOverRail = false;
    if (isRailMenuOpen() || renamingAddress !== null) return;
    closeMenus();
    m.redraw();
  }

  function handlePointerLeftWindow(event: MouseEvent): void {
    if (event.relatedTarget !== null) return;
    handleDocumentPointerLeave();
  }

  function openRowMenu(anchor: MenuAnchor, address: string): void {
    menuRowAddress = address;
    menuError = null;
    rowMenu.open(anchor);
  }

  function openShortcutMenu(anchor: MenuAnchor, key: string): void {
    menuShortcutKey = key;
    menuError = null;
    shortcutMenu.open(anchor);
  }

  /** A rail action: run it, and take down whichever menu was up. */
  function pick(action: () => void): void {
    action();
    for (const menu of railMenus) menu.close();
    menuError = null;
  }

  function commitRename(row: SidebarTabRow, typed: string, attrs: SidebarAttrs): void {
    endRename();
    const title = normalizeTabTitle(typed);
    if (title === null || title === row.label) return;
    attrs.onRenameRow(row, title);
  }

  function beginRename(row: SidebarTabRow): void {
    renamingAddress = row.address;
    renameDraft = row.label;
  }

  function endRename(): void {
    renamingAddress = null;
    renameDraft = "";
    if (!isPointerOverRail) expanded = false;
  }

  /** The rail's own ``TabMenuActions`` for one row. Rename opens the rail's inline editor,
   *  which works for a row with no open tab too. */
  function railMenuActions(row: SidebarTabRow, attrs: SidebarAttrs): TabMenuActions {
    const app = getApp(row.appName);
    return {
      refresh: () => attrs.onRefreshRow(row),
      share: app === undefined || app.critical ? null : () => attrs.onShareApp(row.appName),
      addToProjects: () => attrs.onAddRowToProjects(row),
      rename: () => beginRename(row),
      closeTab: null,
      removeFromProject: isEverythingView(attrs.activeViewId) ? null : () => attrs.onRemoveFromView(row),
      setInstanceLifecycle: (action) => attrs.onInstanceLifecycle(row, action),
      setAppLifecycle: (action) => attrs.onAppLifecycle(row.appName, action),
      delete: () => attrs.onDeleteRow(row),
    };
  }

  interface ShortcutMenuEntry {
    label: string;
    run: () => void;
    isDisabled?: boolean;
  }

  /** The shortcut group every shortcut row's menu carries: the complementary action, then the
   *  mode flip (persisted per project, so Everything offers only the complementary action). */
  function shortcutMenuEntries(resolved: ResolvedShortcut, attrs: SidebarAttrs): ShortcutMenuEntry[] {
    const isEverything = isEverythingView(attrs.activeViewId);
    const entries: ShortcutMenuEntry[] = [];
    if (resolved.mode === "focus") {
      entries.push({ label: resolved.action.label, run: () => attrs.onRunShortcutAsNew(resolved.shortcut) });
    } else {
      entries.push({
        label: `Focus last ${resolved.app.display_name}`,
        run: () => attrs.onFocusLastOfShortcut(resolved.shortcut),
        isDisabled: !attrs.rows.some((row) => row.appName === resolved.app.name),
      });
    }
    if (!isEverything) {
      entries.push({
        label:
          resolved.mode === "focus"
            ? `Change shortcut to "${resolved.action.label}"`
            : `Change shortcut to "${resolved.app.display_name}"`,
        run: () => attrs.onSetShortcutMode(resolved.shortcut, resolved.mode === "focus" ? "new" : "focus"),
      });
    }
    // The app's own Stop and Start live here: the rail row is the app's presence in the view
    // (under Everything every app has one), whereas a tab or a rail row of an instance acts on
    // that instance alone. Offered only for an app the workspace can honestly stop.
    if (isAppStoppable(resolved.app)) {
      const action = resolved.app.is_running ? "stop" : "start";
      entries.push({
        label: `${action === "stop" ? "Stop" : "Start"} ${resolved.app.display_name}`,
        run: () => attrs.onAppLifecycle(resolved.app.name, action),
      });
    }
    return entries;
  }

  function shortcutMenuRows(attrs: SidebarAttrs): MenuRow[] {
    if (menuShortcutKey === null) return [];
    const resolved = effectiveShortcuts(activeProject(attrs), getOpenableApps()).find(
      (candidate) => shortcutKey(candidate.shortcut) === menuShortcutKey,
    );
    if (resolved === undefined) return [];
    const entries = shortcutMenuEntries(resolved, attrs);
    const canUnpin = !isEverythingView(attrs.activeViewId);
    const rows: MenuRow[] = entries.map((entry) => ({
      kind: "action",
      label: entry.label,
      isDisabled: entry.isDisabled,
      tightGap: true,
      extraClass: MENU_ROW_EXTRA,
      onSelect: () => pick(entry.run),
    }));
    if (canUnpin) {
      rows.push({
        kind: "action",
        label: "Unpin",
        tightGap: true,
        extraClass: MENU_ROW_EXTRA,
        onSelect: () => pick(() => attrs.onRemoveShortcut(resolved.shortcut)),
      });
    }
    return rows;
  }

  function activeProject(attrs: SidebarAttrs): ProjectInfo | null {
    return isEverythingView(attrs.activeViewId) ? null : projectForViewId(attrs.projects, attrs.activeViewId);
  }

  async function createNewProject(attrs: SidebarAttrs): Promise<void> {
    const glyph = nextGlyphIndex(attrs.projects.map((project) => project.glyph));
    try {
      const created = await createProject(nextProjectName(attrs.projects), SQUIGGLE_GLYPHS[glyph].color, glyph);
      closeMenus();
      attrs.onProjectsChanged();
      attrs.onProjectCreated(created.id);
    } catch (error) {
      menuError = (error as Error).message;
    }
    m.redraw();
  }

  // ---------- Rail rows ----------

  function railLabel(content: m.Children, extraClass: string): m.Vnode {
    return m(
      "span",
      {
        class:
          `min-w-0 flex-1 truncate pr-1 ${ROW_TEXT_CLASS} whitespace-nowrap transition-opacity duration-(--dur-base) ` +
          `${extraClass} ` +
          (expanded ? "opacity-100" : "opacity-0"),
      },
      content,
    );
  }

  function headerEditButton(project: ProjectInfo): m.Vnode {
    const openSettings = (event: Event): void => {
      event.stopPropagation();
      settingsProject = project;
    };
    return m(
      "span",
      {
        role: "button",
        tabindex: 0,
        // The Button recipe via buttonClass -- the escape hatch, since this control lives inside
        // the header <button> and buttons do not nest -- with railAction's reveal-on-row-hover.
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
        "aria-expanded": switcherMenu.isOpen() ? "true" : "false",
        ...hoverTooltipAttrs("Switch projects", "right"),
        onclick: (event: MouseEvent) => {
          // As wide as the rail's card, hanging off the header's bottom edge.
          const headerRect = anchorForEvent(event);
          const card = (event.currentTarget as HTMLElement).closest(".machine-sidebar");
          const cardRect = card === null ? headerRect : card.getBoundingClientRect();
          menuError = null;
          switcherMenu.open({
            left: cardRect.left,
            right: cardRect.right,
            top: headerRect.top,
            bottom: headerRect.bottom,
            width: cardRect.width,
          });
        },
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          if (project === null) return;
          menuError = null;
          headerMenu.open(anchorForPointer(event));
        },
      },
      [
        m("span", { class: ICON_BOX_CLASS }, m.trust(viewIdentityMarkup(project, ROW_ICON_SIZE))),
        railLabel(viewName, "font-semibold"),
        project !== null && expanded ? headerEditButton(project) : null,
        m(
          "span",
          {
            class:
              "flex shrink-0 items-center pr-1 text-secondary transition-opacity duration-(--dur-base) " +
              (expanded ? "opacity-100" : "opacity-0"),
          },
          m.trust(icon("chevron-down", { size: ACTION_ICON_SIZE })),
        ),
      ],
    );
  }

  /** One shortcut row: the app's glyph and the row's label, with the hover-revealed unpin
   *  and kebab laid over its right edge. */
  function shortcutRow(resolved: ResolvedShortcut, attrs: SidebarAttrs): m.Vnode {
    const key = shortcutKey(resolved.shortcut);
    const isAwaiting = attrs.awaitingActionKeys.has(key);
    const isStopped = !resolved.app.is_running;
    const label = isAwaiting ? "Starting…" : shortcutLabel(resolved);
    const canUnpin = !isEverythingView(attrs.activeViewId);
    const isMenuOpen = shortcutMenu.isOpen() && menuShortcutKey === key;
    const tooltip = isStopped ? `${label} — not running` : label;
    return m(
      "span",
      {
        key: `shortcut:${key}`,
        class:
          "project-rail-shortcut-slot group relative flex w-full shrink-0 items-center rounded-md hover:bg-fill-hover",
        ...hoverTooltipAttrs(tooltip, "right"),
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          openShortcutMenu(anchorForPointer(event), key);
        },
      },
      [
        m(
          "button",
          {
            type: "button",
            disabled: isAwaiting,
            "data-shortcut": key,
            class:
              `project-rail-shortcut ${ROW_CLASS} ` +
              (canUnpin ? "pr-12 " : "pr-7 ") +
              (isAwaiting
                ? "cursor-default text-faint opacity-60"
                : isStopped
                  ? "project-rail-shortcut-stopped text-faint opacity-60"
                  : "text-primary"),
            onclick: isAwaiting ? undefined : () => pick(() => attrs.onRunShortcut(resolved.shortcut)),
          },
          [m("span", { class: ICON_BOX_CLASS }, m.trust(appGlyph(resolved.app, ROW_ICON_SIZE))), railLabel(label, "")],
        ),
        canUnpin
          ? railAction({
              iconMarkup: railIcon("pin", ACTION_ICON_SIZE),
              label: `Unpin ${resolved.app.display_name} from this project`,
              extra: "project-rail-shortcut-unpin absolute right-1",
              onclick: () => attrs.onRemoveShortcut(resolved.shortcut),
            })
          : null,
        railAction({
          iconMarkup: railIcon("kebab", ACTION_ICON_SIZE),
          label: `Shortcut options for ${resolved.app.display_name}`,
          isRevealed: isMenuOpen,
          extra: "project-rail-shortcut-menu absolute " + (canUnpin ? "right-6" : "right-1"),
          onclick: (event) => openShortcutMenu(anchorForEvent(event), key),
        }),
      ],
    );
  }

  function shortcuts(attrs: SidebarAttrs, resolved: readonly ResolvedShortcut[]): m.Vnode {
    return m(
      "div",
      { class: "min-h-0 shrink overflow-x-hidden overflow-y-auto" },
      resolved.map((shortcut) => shortcutRow(shortcut, attrs)),
    );
  }

  function allAppsRow(): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: `project-rail-all-apps ${ROW_CLASS} text-faint hover:bg-fill-hover hover:text-secondary`,
        "aria-haspopup": "menu",
        "aria-expanded": allAppsMenu.isOpen() ? "true" : "false",
        onclick: (event: MouseEvent) => {
          menuError = null;
          allAppsMenu.open(anchorForEvent(event));
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

  function renameRow(row: SidebarTabRow, attrs: SidebarAttrs): m.Vnode {
    return m("div", { key: row.address, class: `${ROW_CLASS} pr-1` }, [
      m("span", { class: ICON_BOX_CLASS }, m.trust(appGlyph(getApp(row.appName), ROW_ICON_SIZE))),
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
          if (renamingAddress !== row.address) return;
          commitRename(row, renameDraft, attrs);
        },
        onkeydown: (event: KeyboardEvent) => {
          if (event.key === "Enter") (event.target as HTMLInputElement).blur();
          else if (event.key === "Escape") endRename();
        },
      }),
    ]);
  }

  function tabRow(row: SidebarTabRow, ranges: readonly MatchRange[], attrs: SidebarAttrs): m.Vnode {
    if (renamingAddress === row.address) return renameRow(row, attrs);
    const isMenuOpenHere = rowMenu.isOpen() && menuRowAddress === row.address;
    return m(
      "div",
      {
        key: row.address,
        "data-address": row.address,
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
            if (row.isOpen) expanded = false;
          }),
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          openRowMenu(anchorForPointer(event), row.address);
        },
      },
      [
        m("span", { class: ICON_BOX_CLASS }, m.trust(appGlyph(getApp(row.appName), ROW_ICON_SIZE))),
        m(
          "span",
          { class: `min-w-0 flex-1 truncate ${ROW_TEXT_CLASS} whitespace-nowrap` },
          matchedLabel(row.label, ranges),
        ),
        railAction({
          iconMarkup: railIcon("kebab", ACTION_ICON_SIZE),
          label: `Actions for ${row.label}`,
          isRevealed: isMenuOpenHere,
          onclick: (event) => openRowMenu(anchorForEvent(event), row.address),
        }),
      ],
    );
  }

  function tabList(attrs: SidebarAttrs): m.Vnode {
    const searchable = attrs.rows.map((row) => ({
      row,
      label: row.label,
      kindWords: [row.appName, row.appDisplayName],
    }));
    const results = searchRows(searchable, searchQuery);
    if (results.length === 0) {
      return m(
        "div",
        { class: `px-2 py-2 ${ROW_TEXT_CLASS} text-faint` },
        attrs.rows.length === 0 ? "Nothing here yet." : "No tabs match that.",
      );
    }
    return m(
      "div",
      { class: "min-h-0 flex-1 overflow-x-hidden overflow-y-auto" },
      results.map((result) => tabRow(result.row.row, result.labelRanges, attrs)),
    );
  }

  // ---------- Floating menus ----------

  function switcherEditButton(
    project: ProjectInfo,
    onOpen: (project: ProjectInfo) => void,
    isStacked: boolean,
  ): m.Vnode {
    return m(
      "button",
      {
        type: "button",
        class: buttonClass("ghost", {
          icon: true,
          xs: true,
          extra:
            "opacity-0 focus-visible:opacity-100 group-hover:opacity-100 " + (isStacked ? "absolute inset-0" : ""),
        }),
        "aria-label": `Edit ${project.name}`,
        ...hoverTooltipAttrs(`Edit ${project.name}`),
        onclick: (event: MouseEvent) => {
          event.stopPropagation();
          onOpen(project);
        },
      },
      m.trust(icon("edit", { size: ACTION_ICON_SIZE, strokeWidth: 1.75 })),
    );
  }

  function switcherRowTrailing(
    isActive: boolean,
    project: ProjectInfo | null,
    onOpen: ((project: ProjectInfo) => void) | null,
  ): m.Vnode | null {
    if (project === null || onOpen === null) {
      return isActive
        ? m(
            "span",
            { class: "project-rail-check flex h-5 w-5 shrink-0 items-center justify-center text-secondary" },
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

  function switcherMenuRows(attrs: SidebarAttrs): MenuRow[] {
    const isEverythingActive = isEverythingView(attrs.activeViewId);
    const rows: MenuRow[] = attrs.projects.map((project) => {
      const isCurrent = project.id === attrs.activeViewId;
      return {
        kind: "action",
        label: project.name,
        icon: { markup: viewIdentityMarkup(project, ROW_ICON_SIZE) },
        iconBoxClass: ICON_BOX_CLASS,
        tightGap: true,
        extraClass: MENU_ROW_EXTRA,
        trailing: switcherRowTrailing(isCurrent, project, (target) =>
          pick(() => {
            settingsProject = target;
          }),
        ),
        onSelect: () => {
          if (isCurrent) return;
          attrs.onSelectView(project.id);
        },
      };
    });
    rows.push({
      kind: "action",
      label: "New project",
      icon: { markup: railIcon("plus", ROW_ICON_SIZE) },
      iconBoxClass: ICON_BOX_CLASS,
      tone: "quiet",
      tightGap: true,
      extraClass: MENU_ROW_EXTRA,
      // Stays up: a create that fails says so on the line under the rows, and closes itself
      // on success.
      keepsOpen: true,
      onSelect: () => {
        void createNewProject(attrs);
      },
    });
    if (menuError !== null) {
      const error = menuError;
      rows.push({
        kind: "custom",
        key: "error",
        render: () => m("div", { class: "px-3 py-1 text-[12px] text-danger" }, error),
      });
    }
    rows.push({ kind: "divider" });
    rows.push({
      kind: "action",
      label: EVERYTHING_VIEW_NAME,
      icon: { markup: compositeSquiggleMarkup(ROW_ICON_SIZE) },
      iconBoxClass: ICON_BOX_CLASS,
      tightGap: true,
      extraClass: MENU_ROW_EXTRA,
      trailing: switcherRowTrailing(isEverythingActive, null, null),
      onSelect: () => attrs.onSelectView(EVERYTHING_VIEW_ID),
    });
    return rows;
  }

  function headerMenuRows(project: ProjectInfo): MenuRow[] {
    return [
      {
        kind: "action",
        label: "Project settings...",
        tightGap: true,
        extraClass: MENU_ROW_EXTRA,
        onSelect: () => {
          settingsProject = project;
        },
      },
    ];
  }

  /** A row's kebab/context menu: the same shared verb set the tab's own kebab renders. */
  function rowMenuRows(attrs: SidebarAttrs): MenuRow[] {
    const row = attrs.rows.find((candidate) => candidate.address === menuRowAddress);
    if (row === undefined) return [];
    const resolved = findInstance(row.address);
    if (resolved === null) return [];
    const entries = tabMenuEntries(resolved.app, resolved.instance, railMenuActions(row, attrs));
    return entries.map((entry) =>
      entry === TAB_MENU_DIVIDER
        ? { kind: "divider" }
        : {
            kind: "action",
            label: entry.label,
            icon: { markup: icon(entry.iconName, { size: ACTION_ICON_SIZE }) },
            tone: entry.isDestructive ? "danger" : "default",
            tightGap: true,
            extraClass: MENU_ROW_EXTRA,
            onSelect: () => pick(entry.run),
          },
    );
  }

  function allAppsMenuRows(attrs: SidebarAttrs, project: ProjectInfo | null): MenuRow[] {
    return [
      {
        kind: "custom",
        key: "picker",
        render: () =>
          m(AllAppsPicker, {
            projectName: project?.name ?? null,
            pinnedKeys: (project?.shortcuts ?? []).map(shortcutKey),
            onRunAction: (app, action) => pick(() => attrs.onRunAppAction(app, action)),
            onPin: (app, action) => {
              // Pinning is not picking: the popover stays open so several rows can be pinned.
              attrs.onPinShortcut(app, action);
            },
          }),
      },
    ];
  }

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
        endRename();
      }
      lastRenderedViewId = attrs.activeViewId;
      if (renamingAddress !== null && !attrs.rows.some((row) => row.address === renamingAddress)) endRename();
      const isEverything = isEverythingView(attrs.activeViewId);
      const project = attrs.projects.find((candidate) => candidate.id === attrs.activeViewId) ?? null;
      const viewName = isEverything ? EVERYTHING_VIEW_NAME : (project?.name ?? "");
      const resolvedShortcuts = effectiveShortcuts(project, getOpenableApps());

      return m(
        "div",
        {
          class: "relative w-[37px] shrink-0",
          oncreate: () => {
            document.addEventListener("mouseout", handlePointerLeftWindow);
            document.documentElement.addEventListener("mouseleave", handleDocumentPointerLeave);
            window.addEventListener("blur", handleWindowBlur);
          },
          onremove: () => {
            for (const menu of railMenus) menu.dispose();
            document.removeEventListener("mouseout", handlePointerLeftWindow);
            document.documentElement.removeEventListener("mouseleave", handleDocumentPointerLeave);
            window.removeEventListener("blur", handleWindowBlur);
          },
          onmouseenter: () => {
            isPointerOverRail = true;
            expanded = true;
          },
          onmouseleave: () => {
            isPointerOverRail = false;
            if (isRailMenuOpen() || renamingAddress !== null) return;
            closeMenus();
          },
        },
        [
          m(
            "div",
            {
              class:
                "machine-sidebar absolute top-[1px] bottom-[4px] left-0 z-20 flex flex-col overflow-hidden border " +
                `${RAIL_PADDING_CLASS} transition-[width] duration-(--dur-base) ease-out ` +
                (expanded ? EXPANDED_CLASS : COLLAPSED_CLASS),
            },
            [
              header(project, viewName),
              m("div", { class: `${DIVIDER_CLASS} mb-1 ` + (expanded ? "border-default" : "border-transparent") }),
              shortcuts(attrs, resolvedShortcuts),
              expanded ? allAppsRow() : null,
              expanded ? m("div", { class: `${DIVIDER_CLASS} mt-1` }) : null,
              expanded ? searchPill(viewName) : null,
              expanded ? tabList(attrs) : null,
            ],
          ),
          switcherMenu.view(switcherMenuRows(attrs)),
          project === null ? null : headerMenu.view(headerMenuRows(project)),
          allAppsMenu.view(allAppsMenuRows(attrs, project)),
          rowMenu.view(rowMenuRows(attrs)),
          shortcutMenu.view(shortcutMenuRows(attrs)),
          settingsModal(attrs),
        ],
      );
    },
  };
}
