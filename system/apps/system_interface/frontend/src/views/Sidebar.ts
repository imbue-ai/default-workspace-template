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
import { findInstance, getApp, getOpenableApps, primaryActionForApp } from "../models/Inventory";
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
import { Button, buttonClass } from "./components/Button";
import { hoverTooltipAttrs } from "./components/hoverTooltip";
import type { TooltipPlacement } from "./components/hoverTooltip";
import { icon } from "./components/icons";
import { menuCardClass, menuDividerClass, menuRowClass } from "./components/menu";
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
  label: string;
  // Whether the instance has a tab in the dock right now. Open rows read as primary text,
  // backgrounded ones (listed, just not docked) as tertiary.
  isOpen: boolean;
  status: InstanceStatus;
  renameable: boolean;
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

const MENU_CARD_CLASS = `project-rail-menu ${menuCardClass(`fixed ${ROW_TEXT_CLASS} text-primary`)}`;
// tightGap (4px) matches the rail's own rows (ROW_CLASS), so a menu row reads as tight as the
// rail row sitting right above it.
const MENU_ROW_CLASS = `project-rail-menu-item group ${menuRowClass({ tightGap: true })}`;
// The scrim shares the menu card's --z-dropdown layer; the card stays on top because it
// renders after the scrim as a sibling. `project-rail-menu-scrim` is a bare marker.
const MENU_SCRIM_CLASS = "project-rail-menu-scrim fixed inset-0 z-(--z-dropdown)";
const MENU_MARGIN = 6;
const SWITCHER_MENU_WIDTH = 256;
const SWITCHER_ROW_CLASS =
  "project-rail-menu-item group flex h-8 w-full cursor-pointer items-center gap-1 pl-[5px] pr-3 text-left " +
  "hover:bg-fill-hover";

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

// ---------- Floating menu placement ----------

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
 * Where a floating menu goes: hanging under its anchor ("below") or beside it ("right"). Either
 * way it flips to the opposite side when it would overflow the window and there is room on the
 * other one, then clamps MENU_MARGIN from the edges.
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
    left: Math.max(Math.min(MENU_MARGIN, anchor.left), Math.min(left, viewport.width - MENU_MARGIN - size.width)),
    top: Math.max(MENU_MARGIN, Math.min(top, viewport.height - MENU_MARGIN - size.height)),
  };
}

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

/** What a shortcut row reads: the action's label while it always creates ("New Chat"), the
 *  app's name while it focuses ("Chat"). */
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

type OpenMenu =
  | { kind: "switcher"; anchor: MenuAnchor }
  | { kind: "header"; anchor: MenuAnchor }
  | { kind: "allApps"; anchor: MenuAnchor }
  | { kind: "row"; anchor: MenuAnchor; address: string }
  | { kind: "shortcut"; anchor: MenuAnchor; key: string };

function shortcutKey(shortcut: ProjectShortcut): string {
  return `${shortcut.app}:${shortcut.action}`;
}

export function Sidebar(): m.Component<SidebarAttrs> {
  let expanded = false;
  let openMenu: OpenMenu | null = null;
  let settingsProject: ProjectInfo | null = null;
  let renamingAddress: string | null = null;
  let renameDraft = "";
  let isPointerOverRail = false;
  let searchQuery = "";
  let menuError: string | null = null;
  let rootElement: HTMLElement | null = null;
  let lastRenderedViewId: string | null = null;

  function isAnyMenuOpen(): boolean {
    return openMenu !== null || settingsProject !== null;
  }

  function closeMenus(): void {
    openMenu = null;
    menuError = null;
    expanded = false;
  }

  function handleOutsideMousedown(event: MouseEvent): void {
    if (!isAnyMenuOpen()) return;
    if (rootElement !== null && !rootElement.contains(event.target as Node)) {
      closeMenus();
      m.redraw();
    }
  }

  /** Close on the window losing focus, which is how a click into a cross-origin pane is seen. */
  function handleWindowBlur(): void {
    isPointerOverRail = false;
    if (renamingAddress !== null) return;
    if (!isAnyMenuOpen() && !expanded) return;
    closeMenus();
    m.redraw();
  }

  function handleDocumentPointerLeave(): void {
    isPointerOverRail = false;
    if (openMenu !== null || renamingAddress !== null) return;
    closeMenus();
    m.redraw();
  }

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

  function pick(action: () => void): void {
    action();
    openMenu = null;
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
   *  which works for a backgrounded row too. */
  function railMenuActions(row: SidebarTabRow, attrs: SidebarAttrs): TabMenuActions {
    const app = getApp(row.appName);
    return {
      refresh: () => attrs.onRefreshRow(row),
      share: app === undefined || app.critical ? null : () => attrs.onShareApp(row.appName),
      addToProjects: () => attrs.onAddRowToProjects(row),
      rename: () => beginRename(row),
      closeTab: null,
      removeFromProject: isEverythingView(attrs.activeViewId) ? null : () => attrs.onRemoveFromView(row),
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
    return entries;
  }

  function shortcutMenu(attrs: SidebarAttrs, menu: Extract<OpenMenu, { kind: "shortcut" }>): m.Children {
    const resolved = effectiveShortcuts(activeProject(attrs), getOpenableApps()).find(
      (candidate) => shortcutKey(candidate.shortcut) === menu.key,
    );
    if (resolved === undefined) return null;
    const entries = shortcutMenuEntries(resolved, attrs);
    const canUnpin = !isEverythingView(attrs.activeViewId);
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
              onclick: () => pick(() => attrs.onRemoveShortcut(resolved.shortcut)),
            })
          : null,
      ],
    });
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
        "aria-expanded": openMenu?.kind === "switcher" ? "true" : "false",
        ...hoverTooltipAttrs("Switch projects", "right"),
        onclick: (event: MouseEvent) => {
          if (openMenu?.kind === "switcher") {
            openMenu = null;
            return;
          }
          const headerRect = anchorForEvent(event);
          const card = (event.currentTarget as HTMLElement).closest(".machine-sidebar");
          const cardRect = card === null ? headerRect : card.getBoundingClientRect();
          openMenuAt({
            kind: "switcher",
            anchor: {
              left: cardRect.left,
              right: cardRect.right,
              top: headerRect.top,
              bottom: headerRect.bottom,
              width: cardRect.width,
            },
          });
        },
        oncontextmenu: (event: MouseEvent) => {
          event.preventDefault();
          if (project === null) return;
          openMenuAt({ kind: "header", anchor: anchorForPointer(event) });
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
    const isMenuOpen = openMenu?.kind === "shortcut" && openMenu.key === key;
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
          openMenuAt({ kind: "shortcut", anchor: anchorForPointer(event), key });
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
          onclick: (event) => openMenuAt({ kind: "shortcut", anchor: anchorForEvent(event), key }),
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
    const isMenuOpenHere = openMenu?.kind === "row" && openMenu.address === row.address;
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
          openMenuAt({ kind: "row", anchor: anchorForPointer(event), address: row.address });
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
          onclick: (event) => {
            if (isMenuOpenHere) {
              openMenu = null;
              return;
            }
            openMenuAt({ kind: "row", anchor: anchorForEvent(event), address: row.address });
          },
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
        style: `left: 0; top: 0; ${options.width === null ? "" : `width: ${options.width}px;`}`,
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
    isQuiet?: boolean;
    isDisabled?: boolean;
    tooltip?: string | null;
    onclick: (event: MouseEvent) => void;
    trailing?: m.Children;
    rowClass?: string;
    iconBoxClass?: string;
  }): m.Vnode {
    const tone = options.isDisabled
      ? "project-rail-menu-item-disabled text-faint cursor-default hover:bg-transparent"
      : options.isDestructive
        ? "text-danger"
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

  function switcherMenu(attrs: SidebarAttrs, anchor: MenuAnchor): m.Vnode {
    const isEverythingActive = isEverythingView(attrs.activeViewId);
    return floatingCard({
      anchor,
      placement: "below",
      role: "menu",
      width: SWITCHER_MENU_WIDTH,
      children: [
        attrs.projects.map((project) => {
          const isCurrent = project.id === attrs.activeViewId;
          return menuRow({
            iconMarkup: viewIdentityMarkup(project, ROW_ICON_SIZE),
            label: project.name,
            rowClass: SWITCHER_ROW_CLASS,
            iconBoxClass: ICON_BOX_CLASS,
            trailing: switcherRowTrailing(isCurrent, project, (target) =>
              pick(() => {
                settingsProject = target;
              }),
            ),
            onclick: () =>
              pick(() => {
                if (isCurrent) return;
                attrs.onSelectView(project.id);
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
        menuError === null ? null : m("div", { class: "px-3 py-1 text-[12px] text-danger" }, menuError),
        m("div", { class: menuDividerClass() }),
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

  /** A row's kebab/context menu: the same shared verb set the tab's own kebab renders. */
  function rowMenu(attrs: SidebarAttrs, menu: Extract<OpenMenu, { kind: "row" }>): m.Children {
    const row = attrs.rows.find((candidate) => candidate.address === menu.address);
    if (row === undefined) return null;
    const resolved = findInstance(row.address);
    if (resolved === null) return null;
    const entries = tabMenuEntries(resolved.app, resolved.instance, railMenuActions(row, attrs));
    return floatingCard({
      anchor: menu.anchor,
      placement: "right",
      role: "menu",
      width: null,
      children: entries.map((entry) =>
        entry === TAB_MENU_DIVIDER
          ? m("div", { class: menuDividerClass() })
          : menuRow({
              iconMarkup: icon(entry.iconName, { size: ACTION_ICON_SIZE }),
              label: entry.label,
              isDestructive: entry.isDestructive,
              onclick: () => pick(entry.run),
            }),
      ),
    });
  }

  function allAppsMenu(attrs: SidebarAttrs, anchor: MenuAnchor, project: ProjectInfo | null): m.Vnode {
    return floatingCard({
      anchor,
      placement: "right",
      role: "dialog",
      width: null,
      children: m(AllAppsPicker, {
        projectName: project?.name ?? null,
        pinnedKeys: (project?.shortcuts ?? []).map(shortcutKey),
        onRunAction: (app, action) => pick(() => attrs.onRunAppAction(app, action)),
        onPin: (app, action) => {
          // Pinning is not picking: the popover stays open so several rows can be pinned.
          attrs.onPinShortcut(app, action);
        },
      }),
    });
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
            isPointerOverRail = false;
            if (openMenu !== null || renamingAddress !== null) return;
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
          openMenu === null ? null : menuScrim(),
          openMenu?.kind === "switcher" ? switcherMenu(attrs, openMenu.anchor) : null,
          openMenu?.kind === "header" && project !== null ? headerMenu(project, openMenu.anchor) : null,
          openMenu?.kind === "allApps" ? allAppsMenu(attrs, openMenu.anchor, project) : null,
          openMenu?.kind === "row" ? rowMenu(attrs, openMenu) : null,
          openMenu?.kind === "shortcut" ? shortcutMenu(attrs, openMenu) : null,
          settingsModal(attrs),
        ],
      );
    },
  };
}
