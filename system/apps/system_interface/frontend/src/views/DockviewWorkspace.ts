/**
 * The dock: one DockviewComponent holding the tabs of whichever *view* is mounted, plus the
 * bookkeeping that ties those tabs to the machine.
 *
 * Every tab shows one instance of one app, named by its address (``app:<name>`` for a
 * single-instance app, ``app:<name>?instance=<key>`` otherwise), in an iframe the shell sizes
 * to the pane. The shell knows nothing about what any app is: what a tab is called, what icon
 * it wears, what its status is, and whether it can be renamed or deleted all come from the
 * inventory (``models/Inventory``), which the shell's WebSocket keeps current.
 *
 * A view is either a project (a shared tab set plus this client's own arrangement of it) or
 * Everything (every instance on the machine). The rules the dock enforces are narrow:
 *   - opening an instance in a project files its address into the project's tab set;
 *   - closing a tab changes no tab set and stops nothing;
 *   - an instance its app stops listing leaves every tab and tab set (the shell prunes the
 *     saved layouts and tab sets; this dock drops the live panel when the list arrives).
 *
 * The dock is never empty. A view with no panels gets a New Tab launcher, which is also what
 * the "+" opens (a freshly-created project shows it only until its first chat is created).
 */

import m from "mithril";
import {
  DockviewComponent,
  themeLight,
  type DockviewGroupPanel,
  type IContentRenderer,
  type IDockviewPanel,
  type IHeaderActionsRenderer,
  type ITabRenderer,
  type SerializedDockview,
  type TabPartInitParameters,
} from "dockview-core";
import { requestFrameFocus } from "./terminalFocus";
import {
  IFRAME_PANEL_ADDRESS_ATTR,
  IFRAME_PANEL_APP_ATTR,
  IframePanel,
  StoppedAppPlaceholder,
  reloadIframeForAddress,
  reloadIframesForApp,
} from "./IframePanel";
import type { StoppedAppPlaceholderAttrs } from "./IframePanel";
import {
  bindSlot,
  clearReportedPath,
  destroyLiveSurface,
  duplicateLiveKeyPanelIds,
  ensureLiveSurface,
  initializeLiveLayer,
  isPageAtListedUrl,
  liveKeyForPanel,
  liveSurfaceBoundPanelId,
  liveSurfaceElement,
  liveSurfaceKeys,
  reconcileLiveSurfaces,
  recordReportedPath,
  rekeyLiveSurface,
  scheduleReconcile,
  setDragInProgress,
  unbindSlot,
  type LiveSurface,
  type PanelParams,
} from "./liveSurfaces";
import { DestroyConfirmDialog } from "./DestroyConfirmDialog";
import { ProjectMembershipDialog } from "./ProjectMembershipDialog";
import { appIconMarkup } from "./appIcon";
import { NewTabLauncher } from "./NewTabLauncher";
import type { LaunchTile, LauncherRow } from "./NewTabLauncher";
import { TAB_MENU_DIVIDER, tabMenuEntries } from "./tabMenu";
import type { TabMenuActions, TabMenuEntry } from "./tabMenu";
import { placeMenu } from "./Sidebar";
import type { MenuAnchor, SidebarTabRow } from "./Sidebar";
import { normalizeTabTitle } from "./tab-rename";
import { attachHoverTooltip } from "./hoverTooltip";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import { OPEN_SHARE_SETTINGS, sendToEmbedder, setEmbedderMessageHandler } from "../embed";
import { SHELL_CLOSE_REQUEST, SHELL_FOCUSED, SHELL_LOCATION, SHELL_OPEN } from "../app_contract";
import { sendToChildFrame, setChildFrameMessageHandler } from "../relay";
import { reloadInterface } from "../reload";
import { icon } from "./icons";
import type { IconName } from "./icons";
import {
  addAppsUpdatedListener,
  addLayoutOpListener,
  addLoadViewListener,
  addProjectsUpdatedListener,
  addTabReboundListener,
  addressFor,
  appNameFromAddress,
  appStoppedDetail,
  applyProjects,
  findInstance,
  getApp,
  getOpenableApps,
  instancePageUrl,
  isAddressUnlisted,
  isAppStoppable,
  listInstances,
  parseAddress,
  removeAppsUpdatedListener,
  reportClientState,
  primaryActionForApp,
  whenAppsLoaded,
  type AppRecord,
  type LayoutOpEvent,
  type ProjectInfo,
  type ProjectShortcut,
  type ResolvedInstance,
  type ShortcutMode,
  type TabReboundEvent,
} from "../models/Inventory";
import { CHAT_APP_NAME, CHAT_NEW_ACTION } from "../models/chatApp";
import { getActiveProjectId, getClientId, getStoredProjectId, setActiveProjectId } from "../models/ClientIdentity";
import { areAccountsLoaded, getAccounts, loadAccounts, openProviderChooser } from "../models/Providers";
import {
  addProjectTab,
  chooseInitialViewId,
  fetchProjectsList,
  isEverythingView,
  projectForViewId,
  removeProjectShortcut,
  removeProjectTab,
  setProjectShortcut,
} from "../models/Projects";
import { fetchLayout, mintTabId, panelsWithUnlistedAddresses, saveLayout } from "../models/Layouts";
import type { LayoutRecord } from "../models/Layouts";
import type { TabRecord } from "../models/Layouts";
import {
  createInstance,
  deleteInstance,
  renameInstance,
  reportInstanceLocation,
  setAppLifecycle,
} from "../models/Relay";

const AUTOSAVE_DEBOUNCE_MS = 1500;

// A launcher panel's id. The prefix is what tells a launcher apart after a layout restore,
// when all that survives is the panel id and its component name.
const LAUNCHER_PANEL_ID_PREFIX = "new-tab-";
const LAUNCHER_PANEL_TITLE = "New tab";
const INSTANCE_COMPONENT = "instance";
const LAUNCHER_COMPONENT = "launcher";

// The chat app, which two rules here still name (CLEANUP: phase 10 of the workspace app model):
// its ``new`` action takes the provider account the launcher's picker chose, and is diverted to
// the provider chooser when nothing is signed in; and ``self`` in an agent's layout op is the
// requester's own chat instance.

// Second paragraph of the delete confirmation: deleting is not a louder Close.
const DELETE_INSTANCE_DETAILS =
  "It leaves every project that shows it, not just this one. The app itself keeps running.";

// How long an open waits for an address the inventory does not list yet. A create the page
// itself ran (a chat's own create route) lands in the inventory on the next nudge; a create
// the relay ran is in the ``apps_updated`` the shell pushes before it answers, which can still
// reach this window after the answer does.
const AWAIT_ADDRESS_TIMEOUT_MS = 4000;

interface DeleteDialogState {
  address: string;
  label: string;
}

interface MembershipDialogState {
  address: string;
  label: string;
  projectIds: string[];
}

let deleteDialog: DeleteDialogState | null = null;
let membershipDialog: MembershipDialogState | null = null;

// Single shared dockview state
let dockview: DockviewComponent | null = null;
let dockviewContainer: HTMLElement | null = null;
const panelParams = new Map<string, PanelParams>();
// Epoch milliseconds each instance panel was last the active one, for the saved tab record.
const lastFocusedMsByPanelId = new Map<string, number>();
// The panels a restore in progress will remove as soon as ``fromJSON`` has rebuilt the grid:
// their addresses left the inventory, so they get a silent placeholder rather than the
// "could not be restored" warning a genuinely unknown panel earns.
let panelsPrunedByRestore: ReadonlySet<string> = new Set();
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let initialized = false;
// True while a view's content is being mounted. The teardown half of that removes every
// panel one at a time, which must not be mistaken for the user emptying the dock.
let isApplyingLayout = false;

// ---------- Active-view state ----------

// The project registry, as last listed or pushed. Everything is never in here.
let availableProjects: ProjectInfo[] = [];
// The view whose content is currently mounted in the dockview, and so also the view autosave
// writes to -- a project id, or EVERYTHING_VIEW_ID.
let mountedViewId: string | null = null;
// Serialized form of the layout last persisted for the active view; autosave skips the POST
// when the current serialization matches.
let lastPersistedLayoutJson: string | null = null;
// Set when the mounted view's layout could not be fetched: what the dock shows then is not the
// client's arrangement, and saving it would overwrite the real one. Cleared by the next fetch.
let isLayoutSaveSuspended = false;
// Bumped by every mount of a view, the initial one included. A mount whose generation has moved
// on after an await abandons its remaining steps, so two overlapping switches (a double click,
// a load pushed mid-switch) cannot leave one view's arrangement under the other's autosave.
let viewMountGeneration = 0;
// The generation whose content is mounted. Between a switch repointing ``mountedViewId`` and
// its ``applyLayout`` finishing, the dock still shows the outgoing view, and a save then would
// file that arrangement under the incoming view's id; autosave waits for the two to agree.
let settledViewGeneration = 0;

// Target fraction of horizontal space a newly-opened pane takes when it splits alongside the
// requesting agent's chat.
const OPEN_TAB_SPLIT_FRACTION = 0.6;

// ---------- Tabs ----------

// Equal-width tabs. ``TAB_STRIP_RESERVED_PX`` is the space every strip keeps for its "+" and
// the first tab's leading margin; the ideal width is what is left over, divided by the tabs,
// and clamped so a strip full of tabs stays readable and a strip holding one does not stretch
// it across the pane.
const TAB_STRIP_RESERVED_PX = 44;
const TAB_WIDTH_MIN_PX = 140;
const TAB_WIDTH_MAX_PX = 220;
// A title too long for its tab fades out over its last 20px instead of taking an ellipsis.
const TAB_TITLE_FADE_PX = 20;

/** One tab strip's contribution to the shared tab width. */
export interface TabStripMetrics {
  width: number;
  tabCount: number;
}

/**
 * The one width every tab in every strip renders at: the narrowest strip's ideal, clamped to
 * [TAB_WIDTH_MIN_PX, TAB_WIDTH_MAX_PX]. Strips with no tabs are skipped, and a dock with no
 * tabs at all answers with the ceiling.
 */
export function equalTabWidth(strips: readonly TabStripMetrics[]): number {
  let ideal = Number.POSITIVE_INFINITY;
  for (const strip of strips) {
    if (strip.tabCount <= 0) continue;
    ideal = Math.min(ideal, (strip.width - TAB_STRIP_RESERVED_PX) / strip.tabCount);
  }
  if (!Number.isFinite(ideal)) return TAB_WIDTH_MAX_PX;
  return Math.round(Math.min(TAB_WIDTH_MAX_PX, Math.max(TAB_WIDTH_MIN_PX, ideal)));
}

/** Whether a title actually overflows the box it is drawn in, with a one-pixel tolerance for
 *  sub-pixel layout. */
export function isTitleTruncated(scrollWidth: number, clientWidth: number): boolean {
  return scrollWidth > clientWidth + 1;
}

const XMLNS = "http://www.w3.org/2000/svg";

// The launcher tab's plus, and the kebab, on the same 24x24 Feather grid as `icons.ts`.
const TAB_PATHS = {
  launcher: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  app: '<rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>',
  // Filled rather than stroked: at 14px a 1px-radius ring reads as fuzz.
  kebab:
    '<circle cx="12" cy="5" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="12" cy="19" r="1.5" fill="currentColor" stroke="none"/>',
} as const;

type TabIconName = keyof typeof TAB_PATHS;

const TAB_GLYPH_SIZE = 14;

function tabIcon(name: TabIconName, size: number): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${TAB_PATHS[name]}</svg>`
  );
}

/** The markup a tab's leading glyph is drawn from: the app's own icon (or its monogram), so a
 *  tab, its rail row and its launcher row all wear the same picture. */
function tabIconMarkupForPanel(params: PanelParams | undefined): string {
  if (params === undefined || params.kind === "launcher") return tabIcon("launcher", TAB_GLYPH_SIZE);
  const appName = appNameFromAddress(params.address);
  const app = appName === null ? undefined : getApp(appName);
  const fallback = tabIcon("app", TAB_GLYPH_SIZE);
  if (app === undefined) return fallback;
  return appIconMarkup(app.icon, TAB_GLYPH_SIZE, fallback, app.name);
}

// ---------- The tab kebab menu ----------

const TAB_MENU_CARD_CLASS = "fixed z-50 min-w-[180px] rounded-lg border border-border bg-surface py-1 text-[13px]";
const TAB_MENU_SHADOW_STYLE = "box-shadow: 0 1px 1px 0 rgba(0, 0, 0, 0.08), 0 3px 12px 0 rgba(0, 0, 0, 0.08);";
const TAB_MENU_ROW_CLASS = "flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-left hover:bg-bg-hover";

// The one open tab menu, if any.
let openTabMenu: { close: () => void } | null = null;

function closeTabMenu(): void {
  openTabMenu?.close();
}

/**
 * Open a tab's kebab menu against ``anchor``. Built on ``document.body`` rather than inside
 * the tab: the tab strip clips its own overflow. ``trigger`` -- the kebab that opened it -- is
 * excluded from the outside-press close so pressing it again toggles the menu shut.
 */
function openTabMenuAt(
  anchor: MenuAnchor,
  entries: readonly TabMenuEntry[],
  onClosed: () => void,
  trigger: HTMLElement | null,
): void {
  closeTabMenu();
  const element = document.createElement("div");
  element.className = TAB_MENU_CARD_CLASS;
  element.setAttribute("role", "menu");
  element.style.cssText = `left: 0; top: 0; ${TAB_MENU_SHADOW_STYLE}`;

  const close = (): void => {
    document.removeEventListener("pointerdown", onOutsidePointerDown, true);
    document.removeEventListener("keydown", onKeyDown, true);
    window.removeEventListener("resize", close);
    window.removeEventListener("scroll", close, true);
    element.remove();
    openTabMenu = null;
    onClosed();
  };

  function onOutsidePointerDown(event: PointerEvent): void {
    const target = event.target as Node;
    if (element.contains(target) || trigger?.contains(target) === true) return;
    close();
  }

  function onKeyDown(event: KeyboardEvent): void {
    if (event.key === "Escape") close();
  }

  for (const entry of entries) {
    if (entry === TAB_MENU_DIVIDER) {
      const divider = document.createElement("div");
      divider.className = "my-1 border-t border-border";
      element.appendChild(divider);
      continue;
    }
    const row = document.createElement("div");
    row.className = `${TAB_MENU_ROW_CLASS} ${entry.isDestructive ? "text-red-600" : "text-text-primary"}`;
    row.setAttribute("role", "menuitem");
    const glyph = document.createElement("span");
    glyph.className = "flex w-4 shrink-0 items-center justify-center";
    glyph.innerHTML = icon(entry.iconName, { size: 14 });
    const label = document.createElement("span");
    label.className = "min-w-0 flex-1 truncate";
    label.textContent = entry.label;
    row.append(glyph, label);
    row.addEventListener("click", (event) => {
      event.stopPropagation();
      close();
      entry.run();
    });
    element.appendChild(row);
  }

  document.body.appendChild(element);
  const rect = element.getBoundingClientRect();
  const position = placeMenu(
    anchor,
    { width: rect.width, height: rect.height },
    { width: window.innerWidth, height: window.innerHeight },
    "below",
  );
  element.style.left = `${position.left}px`;
  element.style.top = `${position.top}px`;

  document.addEventListener("pointerdown", onOutsidePointerDown, true);
  document.addEventListener("keydown", onKeyDown, true);
  window.addEventListener("resize", close);
  window.addEventListener("scroll", close, true);
  openTabMenu = { close };
}

/** The instance a panel shows, resolved against the inventory, or null for a launcher or an
 *  address the machine no longer lists. */
function resolvedInstanceForPanel(panelId: string): ResolvedInstance | null {
  const params = panelParams.get(panelId);
  if (params === undefined || params.kind === "launcher") return null;
  return findInstance(params.address);
}

/** The verb list of one tab's kebab menu: the shared set (tabMenu.ts) over closures on THIS
 *  live, open panel. A launcher never reaches here. */
function tabMenuEntriesForPanel(panelId: string): TabMenuEntry[] {
  const resolved = resolvedInstanceForPanel(panelId);
  if (resolved === null) {
    return [
      {
        label: "Close tab",
        iconName: "close",
        run: () => dockview?.panels.find((candidate) => candidate.id === panelId)?.api.close(),
      },
    ];
  }
  const actions: TabMenuActions = {
    refresh: () => refreshPanelContent(panelId),
    share: shareActionForApp(resolved.app),
    addToProjects: () => openMembershipDialog(resolved.address, resolved.instance.title),
    rename: () => tabHandlesByPanelId.get(panelId)?.beginTitleEdit(),
    closeTab: () => dockview?.panels.find((candidate) => candidate.id === panelId)?.api.close(),
    // The tab never offers this: unfiling is what you want while looking at the project's list
    // of what it shows; the rail's row menu carries it.
    removeFromProject: null,
    setAppLifecycle: (action) => requestAppLifecycle(resolved.app.name, action),
    delete: () => openDeleteDialog(resolved.address, resolved.instance.title),
  };
  return tabMenuEntries(resolved.app, resolved.instance, actions);
}

/** Ask the embedding minds chrome to open its Share tab for an app. A critical app (the shell
 *  itself, the chat) has no share surface. */
function shareActionForApp(app: AppRecord): (() => void) | null {
  if (app.critical) return null;
  return () => sendToEmbedder(OPEN_SHARE_SETTINGS, { serviceName: app.name });
}

export function shareApp(appName: string): void {
  const app = getApp(appName);
  if (app === undefined) return;
  shareActionForApp(app)?.();
}

/** Fire one stop/start at the shell, surfacing a refusal. The ``apps_updated`` push the route
 *  triggers is what repaints every surface -- nothing optimistic. */
export function requestAppLifecycle(appName: string, action: "stop" | "start"): void {
  void setAppLifecycle(appName, action).catch((e: Error) => {
    alert(`Failed to ${action} ${appName}: ${e.message}`);
  });
}

/** What a pane shows in place of a not-running app's page, or null while the app runs. The
 *  Start button is offered only where the workspace can honestly start the app. */
export function stoppedPlaceholderForApp(app: AppRecord): StoppedAppPlaceholderAttrs | null {
  if (app.is_running) return null;
  return {
    label: app.display_name,
    detail: appStoppedDetail(app),
    onStart: isAppStoppable(app) ? () => requestAppLifecycle(app.name, "start") : null,
  };
}

/** Open the membership dialog over one address, with the projects already showing it. */
function openMembershipDialog(address: string, label: string): void {
  const projectIds = availableProjects
    .filter((project) => project.tabs.includes(address))
    .map((project) => project.id);
  membershipDialog = { address, label, projectIds };
  m.redraw();
}

/** "Add to project..." for a rail row. */
export function addAddressToProjects(address: string): void {
  const resolved = findInstance(address);
  openMembershipDialog(address, resolved?.instance.title ?? address);
}

async function applyMembershipSelection(dialog: MembershipDialogState, selectedProjectIds: string[]): Promise<void> {
  try {
    for (const projectId of selectedProjectIds) {
      await addProjectTab(projectId, dialog.address);
    }
    await refreshProjectsList();
  } catch (e) {
    alert(`Failed to update the projects showing ${dialog.label}: ${(e as Error).message}`);
  }
  m.redraw();
}

function openDeleteDialog(address: string, label: string): void {
  deleteDialog = { address, label };
  m.redraw();
}

/** "Delete" for a rail row: confirm-gated, then the app deletes the instance. */
export function deleteAddress(address: string): void {
  const resolved = findInstance(address);
  if (resolved === null || !resolved.app.has_instances) return;
  openDeleteDialog(address, resolved.instance.title);
}

async function executeDelete(address: string): Promise<void> {
  const parsed = parseAddress(address);
  if (parsed === null) return;
  try {
    await deleteInstance(parsed.app, parsed.key);
  } catch (e) {
    alert(`Failed to delete: ${(e as Error).message}`);
    return;
  }
  // The shell refetches the app's list before answering, so the ``apps_updated`` that follows
  // drops the panel everywhere; dropping it here too keeps this client from waiting a redraw.
  dropPanelsForAddress(address);
  m.redraw();
}

// ---------- The tab ----------

/** The live tabs, so the width recompute can size them and a "+" can flash the launcher its
 *  pane already holds. */
const tabHandlesByPanelId = new Map<
  string,
  { element: HTMLElement; refreshTitleFade: () => void; beginTitleEdit: () => void }
>();

/**
 * One tab: the app's glyph, the instance's status dot, the title, then the right-aligned close
 * and kebab that hover reveals.
 *
 * Double-clicking the title renames the instance when its app renames (``renameable`` on the
 * record): the title becomes a text field, Enter and blur commit through the shell's relay,
 * Escape puts the old one back. Everything else about the tab -- its title, its status -- is
 * read off the inventory and re-synced on every push.
 */
function createCustomTab(options: { id: string; name: string }): ITabRenderer {
  const element = document.createElement("div");
  element.className = "dv-default-tab dv-custom-tab";

  const statusDot = document.createElement("span");
  statusDot.className = "dv-tab-process-dot";
  statusDot.style.display = "none";
  element.appendChild(statusDot);

  const kindIcon = document.createElement("span");
  kindIcon.className = "dv-custom-tab-icon";
  kindIcon.style.display = "flex";
  kindIcon.style.flexShrink = "0";
  kindIcon.style.alignItems = "center";
  element.appendChild(kindIcon);

  const content = document.createElement("div");
  content.className = "dv-default-tab-content";
  content.style.overflow = "hidden";
  content.style.whiteSpace = "nowrap";
  content.style.textOverflow = "clip";
  element.appendChild(content);

  const editor = document.createElement("input");
  editor.type = "text";
  editor.className = "dv-custom-tab-title-input";
  editor.spellcheck = false;
  editor.setAttribute("aria-label", "Tab name");
  editor.style.display = "none";
  element.appendChild(editor);

  const actions = document.createElement("div");
  actions.className = "dv-custom-tab-actions";
  actions.style.display = "none";
  element.appendChild(actions);

  const disposables: Array<{ dispose: () => void }> = [];
  let isPointerOver = false;
  let isMenuOpen = false;
  let isEditingTitle = false;
  // Whether this instance is a row in the tab-overflow dropdown rather than a tab on the strip.
  let isOverflowRow = false;
  let wasTabDraggable: boolean | null = null;

  const refreshTitleFade = (): void => {
    const mask = isTitleTruncated(content.scrollWidth, content.clientWidth)
      ? `linear-gradient(to right, #000 calc(100% - ${TAB_TITLE_FADE_PX}px), transparent 100%)`
      : "";
    content.style.maskImage = mask;
    content.style.webkitMaskImage = mask;
  };

  const updateActionsVisibility = (): void => {
    actions.style.display = !isEditingTitle && (isPointerOver || isMenuOpen) ? "flex" : "none";
    refreshTitleFade();
  };

  const isRenameable = (): boolean => resolvedInstanceForPanel(options.id)?.instance.renameable === true;

  const tabElement = (): HTMLElement | null => element.closest(".dv-tab");

  const beginTitleEdit = (): void => {
    if (isEditingTitle || !isRenameable()) return;
    isEditingTitle = true;
    editor.value = content.textContent ?? "";
    content.style.display = "none";
    editor.style.display = "block";
    // dockview marks every tab draggable, and a draggable ancestor swallows the press-and-sweep
    // that places a caret; the drag stands down for the length of the edit.
    const tab = tabElement();
    if (tab !== null) {
      wasTabDraggable = tab.draggable;
      tab.draggable = false;
    }
    updateActionsVisibility();
    editor.focus();
    editor.select();
  };

  const endTitleEdit = (isCommitting: boolean): void => {
    if (!isEditingTitle) return;
    isEditingTitle = false;
    const typed = editor.value;
    editor.style.display = "none";
    content.style.display = "";
    const tab = tabElement();
    if (tab !== null && wasTabDraggable !== null) tab.draggable = wasTabDraggable;
    wasTabDraggable = null;
    updateActionsVisibility();
    if (!isCommitting) return;
    const title = normalizeTabTitle(typed);
    if (title === null || title === content.textContent) return;
    const params = panelParams.get(options.id);
    if (params === undefined || params.kind === "launcher") return;
    renameAddress(params.address, title);
  };

  content.addEventListener("dblclick", (event) => {
    if (isOverflowRow || !isRenameable()) return;
    event.preventDefault();
    event.stopPropagation();
    beginTitleEdit();
  });
  editor.addEventListener("pointerdown", (event) => event.stopPropagation());
  editor.addEventListener("dblclick", (event) => event.stopPropagation());
  editor.addEventListener("contextmenu", (event) => event.stopPropagation());
  editor.addEventListener("keydown", (event) => {
    event.stopPropagation();
    if (event.key === "Enter") {
      event.preventDefault();
      endTitleEdit(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      endTitleEdit(false);
    }
  });
  editor.addEventListener("blur", () => {
    endTitleEdit(true);
  });

  const statusDotTooltip = attachHoverTooltip(statusDot);
  const updateStatusDot = (): void => {
    const resolved = resolvedInstanceForPanel(options.id);
    if (resolved === null) {
      statusDot.style.display = "none";
      statusDotTooltip.setText(null);
      return;
    }
    statusDot.style.display = "";
    statusDot.setAttribute("data-status", resolved.instance.status);
    statusDotTooltip.setText(resolved.instance.status);
  };

  return {
    element,
    init(parameters: TabPartInitParameters) {
      content.textContent = parameters.api.title ?? parameters.title ?? "";
      kindIcon.innerHTML = tabIconMarkupForPanel(panelParams.get(options.id));
      disposables.push(
        parameters.api.onDidTitleChange((event) => {
          content.textContent = event.title ?? "";
          refreshTitleFade();
        }),
      );
      const params = panelParams.get(options.id);
      const isLauncher = params === undefined || params.kind === "launcher";
      if (!isLauncher) {
        updateStatusDot();
        const statusListener = (): void => updateStatusDot();
        addAppsUpdatedListener(statusListener);
        disposables.push({ dispose: () => removeAppsUpdatedListener(statusListener) });
        disposables.push(statusDotTooltip);
      }

      // An overflow-dropdown row is just the tab: none of the strip's machinery.
      if (parameters.tabLocation === "headerOverflow") {
        isOverflowRow = true;
        actions.remove();
        return;
      }

      const hideButton = createTabActionButton("Close tab", "close", disposables, () => {
        parameters.api.close();
      });

      // A launcher tab is a question about this pane, not an instance: it carries only the hide.
      if (!isLauncher) {
        const openMenu = (anchor: MenuAnchor, trigger: HTMLElement | null): void => {
          if (isMenuOpen) {
            closeTabMenu();
            return;
          }
          isMenuOpen = true;
          updateActionsVisibility();
          openTabMenuAt(
            anchor,
            tabMenuEntriesForPanel(options.id),
            () => {
              isMenuOpen = false;
              updateActionsVisibility();
            },
            trigger,
          );
        };
        const menuButton = createTabActionButton("Tab options", "kebab", disposables, () => {
          openMenu(menuButton.getBoundingClientRect(), menuButton);
        });
        actions.appendChild(menuButton);
        element.addEventListener("contextmenu", (event: MouseEvent) => {
          event.preventDefault();
          openMenu(
            { left: event.clientX, right: event.clientX, top: event.clientY, bottom: event.clientY, width: 0 },
            null,
          );
        });
      }

      actions.appendChild(hideButton);

      element.addEventListener("mouseenter", () => {
        isPointerOver = true;
        updateActionsVisibility();
      });
      element.addEventListener("mouseleave", () => {
        isPointerOver = false;
        updateActionsVisibility();
      });
      updateActionsVisibility();
      tabHandlesByPanelId.set(options.id, { element, refreshTitleFade, beginTitleEdit });
    },
    dispose() {
      if (!isOverflowRow) tabHandlesByPanelId.delete(options.id);
      for (const d of disposables) {
        d.dispose();
      }
      disposables.length = 0;
    },
  };
}

/** One of a tab's two hover-revealed buttons. */
function createTabActionButton(
  title: string,
  iconName: IconName | "kebab",
  disposables: Array<{ dispose: () => void }>,
  onClick: (ev: MouseEvent) => void,
): HTMLButtonElement {
  const button = document.createElement("button");
  button.className = "dv-custom-tab-action";
  button.setAttribute("aria-label", title);
  button.innerHTML = iconName === "kebab" ? tabIcon("kebab", 14) : icon(iconName);
  const tooltip = attachHoverTooltip(button);
  tooltip.setText(title);
  disposables.push(tooltip);
  button.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
  });
  button.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick(ev);
  });
  return button;
}

// ---------- Equal-width tabs ----------

let tabWidthFrame: number | null = null;
let tabStripObserver: ResizeObserver | null = null;
const observedTabStrips = new Set<HTMLElement>();

function observeTabStrips(headers: readonly HTMLElement[]): void {
  if (tabStripObserver === null) return;
  const current = new Set(headers);
  for (const observed of observedTabStrips) {
    if (current.has(observed)) continue;
    tabStripObserver.unobserve(observed);
    observedTabStrips.delete(observed);
  }
  for (const header of headers) {
    if (observedTabStrips.has(header)) continue;
    tabStripObserver.observe(header);
    observedTabStrips.add(header);
  }
}

function tabStripHeaders(): HTMLElement[] {
  if (dockviewContainer === null) return [];
  return Array.from(dockviewContainer.querySelectorAll<HTMLElement>(".dv-tabs-and-actions-container"));
}

function recomputeTabWidths(): void {
  const headers = tabStripHeaders();
  if (headers.length === 0) return;
  observeTabStrips(headers);
  const metrics: TabStripMetrics[] = [];
  const tabElements: HTMLElement[] = [];
  for (const header of headers) {
    const tabs = Array.from(header.querySelectorAll<HTMLElement>(".dv-tabs-container > .dv-tab"));
    metrics.push({ width: header.clientWidth, tabCount: tabs.length });
    tabElements.push(...tabs);
  }
  const width = `${equalTabWidth(metrics)}px`;
  for (const tab of tabElements) {
    tab.style.width = width;
  }
  for (const handle of tabHandlesByPanelId.values()) {
    handle.refreshTitleFade();
  }
}

function scheduleTabWidthRecompute(): void {
  if (tabWidthFrame !== null) return;
  tabWidthFrame = requestAnimationFrame(() => {
    tabWidthFrame = null;
    recomputeTabWidths();
  });
}

function redrawForVisibility(): void {
  m.redraw();
}

/** Placement options that tab a newly-added panel into ``targetGroup`` instead of letting
 *  dockview fall back to the currently-active group. */
function placementForGroup(targetGroup: DockviewGroupPanel | null | undefined): AddPanelPlacementOptions {
  if (targetGroup && dockview?.groups.some((g) => g.id === targetGroup.id)) {
    return { position: { referenceGroup: targetGroup.id } };
  }
  return {};
}

// ---------- The "+" and the New Tab launcher ----------

function groupForPanel(panelId: string): DockviewGroupPanel | null {
  return dockview?.panels.find((panel) => panel.id === panelId)?.api.group ?? null;
}

function launcherPanelIdInGroup(group: DockviewGroupPanel | null): string | null {
  if (!dockview || group === null) return null;
  for (const panel of dockview.panels) {
    if (panelParams.get(panel.id)?.kind !== "launcher") continue;
    if (panel.api.group.id === group.id) return panel.id;
  }
  return null;
}

/** Launchers whose "Open new" tile is waiting on a create, by panel id. */
const launchersAwaitingCreate = new Set<string>();

export function isLauncherAwaitingCreate(panelId: string): boolean {
  return launchersAwaitingCreate.has(panelId);
}

const TAB_FLASH_CLASS = "si-tab-flash";

/** Flash a tab to answer a click that would otherwise look like nothing. */
function flashPanelTab(panelId: string): void {
  const tab = tabHandlesByPanelId.get(panelId)?.element.closest(".dv-tab");
  if (!(tab instanceof HTMLElement)) return;
  tab.classList.remove(TAB_FLASH_CLASS);
  void tab.offsetWidth;
  tab.classList.add(TAB_FLASH_CLASS);
  tab.addEventListener("animationend", () => tab.classList.remove(TAB_FLASH_CLASS), { once: true });
}

/** Open a New Tab launcher in ``targetGroup``, focusing and flashing the one already there
 *  instead of stacking a second. */
function openLauncherPanel(targetGroup: DockviewGroupPanel | null): string | null {
  if (!dockview) return null;
  const existingPanelId = launcherPanelIdInGroup(targetGroup);
  if (existingPanelId !== null) {
    const existing = dockview.panels.find((panel) => panel.id === existingPanelId);
    if (existing) dockview.setActivePanel(existing);
    flashPanelTab(existingPanelId);
    return existingPanelId;
  }
  const panelId = `${LAUNCHER_PANEL_ID_PREFIX}${mintTabId()}`;
  const params: PanelParams = { kind: "launcher" };
  panelParams.set(panelId, params);
  dockview.addPanel({
    id: panelId,
    component: LAUNCHER_COMPONENT,
    title: LAUNCHER_PANEL_TITLE,
    params,
    ...placementForGroup(targetGroup),
  });
  return panelId;
}

/** Retire the launcher a just-opened tab was asked for from. */
function retireLauncher(panelId: string | null): void {
  if (panelId === null || !dockview) return;
  if (panelParams.get(panelId)?.kind !== "launcher") return;
  const panel = dockview.panels.find((candidate) => candidate.id === panelId);
  if (panel) dockview.removePanel(panel);
}

// The one focus change that must NOT fold launchers away: revealing an instance the view
// already had open. One-shot: cleared on the very next focus change.
let revealedOpenPanelId: string | null = null;

/** A launcher is a question and clicking off to some other tab is an answer: every launcher
 *  folds up the moment a real panel takes focus. */
function retireLaunchersOnFocusLeaving(activePanelId: string): void {
  if (!dockview) return;
  const revealedPanelId = revealedOpenPanelId;
  revealedOpenPanelId = null;
  if (revealedPanelId === activePanelId) return;
  if (panelParams.get(activePanelId)?.kind === "launcher") return;
  for (const panel of [...dockview.panels]) {
    if (panel.id !== activePanelId && panelParams.get(panel.id)?.kind === "launcher") {
      dockview.removePanel(panel);
    }
  }
}

/** Grant a just-activated pane's page focus: clicking a tab is the user navigating to it, and
 *  a page (a terminal) never takes focus on its own. */
function focusFrameOfActivatedPanel(activePanelId: string): void {
  const key = liveKeyForPanel(panelParams.get(activePanelId));
  if (key === null) return;
  requestFrameFocus(liveSurfaceElement(key));
}

/** Keep the dock from ever being empty. Suppressed while a layout is being mounted. */
function ensureDockIsNotEmpty(): void {
  if (isApplyingLayout || !dockview) return;
  if (dockview.panels.length > 0) return;
  openLauncherPanel(null);
}

function createAddTabButton(group: DockviewGroupPanel): IHeaderActionsRenderer {
  const element = document.createElement("div");
  element.className = "dockview-add-tab-wrapper";

  const button = document.createElement("button");
  button.className = "dockview-add-tab-button";
  button.setAttribute("aria-label", LAUNCHER_PANEL_TITLE);
  button.textContent = "+";
  const tooltip = attachHoverTooltip(button);
  tooltip.setText(LAUNCHER_PANEL_TITLE);
  element.appendChild(button);

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    openLauncherPanel(group);
  });

  const refreshVisibility = (): void => {
    requestAnimationFrame(() => {
      element.style.display = launcherPanelIdInGroup(group) === null ? "" : "none";
    });
  };

  const subscriptions: { dispose: () => void }[] = [];

  return {
    element,
    init() {
      refreshVisibility();
      if (dockview) {
        subscriptions.push(
          dockview.api.onDidAddPanel(refreshVisibility),
          dockview.api.onDidRemovePanel(refreshVisibility),
          dockview.api.onDidLayoutChange(refreshVisibility),
        );
      }
    },
    dispose() {
      tooltip.dispose();
      for (const subscription of subscriptions) {
        subscription.dispose();
      }
      subscriptions.length = 0;
    },
  };
}

/** The tiles the launcher offers: one per openable app, running its primary action. */
function launchTiles(): LaunchTile[] {
  const tiles: LaunchTile[] = [];
  for (const app of getOpenableApps()) {
    const action = primaryActionForApp(app);
    if (action === null) continue;
    tiles.push({ app, action });
  }
  return tiles;
}

/** Content renderer for a launcher tab. Its attrs are read on every redraw so the tables
 *  follow the machine while it sits open. */
function createLauncherRenderer(panelId: string): IContentRenderer {
  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  return {
    element,
    init() {
      m.mount(element, {
        view: () =>
          m(NewTabLauncher, {
            tiles: launchTiles(),
            rows: launcherRows(),
            memberRows: launcherMemberRows(),
            isEverything: mountedViewId !== null && isEverythingView(mountedViewId),
            isAwaitingCreate: isLauncherAwaitingCreate(panelId),
            onRunAction: (app: AppRecord, actionId: string, params: Record<string, string>) => {
              void runActionInPane(app, actionId, params, groupForPanel(panelId), panelId);
            },
            onOpenRow: (row: LauncherRow) => {
              const openPanelId = panelIdForAddress(row.address);
              if (openPanelId !== null) {
                flashPanelTab(openPanelId);
                return;
              }
              if (openAddressInGroup(row.address, groupForPanel(panelId)) !== null) retireLauncher(panelId);
            },
          }),
      });
    },
    dispose() {
      m.mount(element, null);
    },
  };
}

function launcherRowFor(resolved: ResolvedInstance): LauncherRow {
  return {
    address: resolved.address,
    appName: resolved.app.name,
    appDisplayName: resolved.app.display_name,
    label: resolved.instance.title,
    status: resolved.instance.status,
    lastActiveMs: resolved.instance.last_active === null ? null : Date.parse(resolved.instance.last_active),
  };
}

/** Every instance on the machine, as launcher rows. */
function launcherRows(): LauncherRow[] {
  return listInstances().map(launcherRowFor);
}

/** The active project's tab set as launcher rows (every sidebar row resolves: the rows are
 *  built from the inventory). Everything ignores these. */
function launcherMemberRows(): LauncherRow[] {
  return getSidebarRows().flatMap((row) => {
    const resolved = findInstance(row.address);
    return resolved === null ? [] : [launcherRowFor(resolved)];
  });
}

// ---------- The machine, as the sidebar and the launcher see it ----------

export function getAvailableProjects(): ProjectInfo[] {
  return availableProjects;
}

/** The mounted view: a project id, or EVERYTHING_VIEW_ID. Empty only before the registry has loaded. */
export function getActiveViewId(): string {
  return mountedViewId ?? "";
}

export function refreshProjects(): void {
  void refreshProjectsList();
}

/**
 * The active view's tab list: every instance it holds, open or backgrounded.
 *
 * A project lists its tab set, resolved against the inventory (an address the machine no
 * longer lists is skipped: the shell prunes it from the tab set on the same observation).
 * Everything lists the machine.
 */
export function getSidebarRows(): SidebarTabRow[] {
  const viewId = mountedViewId;
  if (viewId === null) return [];
  const resolvedRows = isEverythingView(viewId)
    ? listInstances()
    : (projectForViewId(availableProjects, viewId)?.tabs ?? [])
        .map((address) => findInstance(address))
        .filter((resolved): resolved is ResolvedInstance => resolved !== null);
  return resolvedRows.map((resolved) => ({
    address: resolved.address,
    appName: resolved.app.name,
    appDisplayName: resolved.app.display_name,
    label: resolved.instance.title,
    isOpen: panelIdForAddress(resolved.address) !== null,
    status: resolved.instance.status,
    renameable: resolved.instance.renameable,
    stoppedDetail: resolved.app.is_running ? undefined : appStoppedDetail(resolved.app),
  }));
}

/**
 * Focus the tab an address already has, or open one for it in the active pane. Flashes the
 * tab when it was already open, so the click visibly does something.
 */
function openAddressInGroup(address: string, targetGroup: DockviewGroupPanel | null): string | null {
  if (!dockview) return null;
  const openPanelId = panelIdForAddress(address);
  revealedOpenPanelId = null;
  if (openPanelId !== null) {
    revealedOpenPanelId = openPanelId;
    const panel = dockview.panels.find((candidate) => candidate.id === openPanelId);
    if (panel) dockview.setActivePanel(panel);
    flashPanelTab(openPanelId);
    return openPanelId;
  }
  return addPanelForAddress(address, placementForGroup(targetGroup));
}

/** Rail row / launcher row click: focus the instance's tab, or open it into the active pane. */
export function openAddress(address: string): void {
  openAddressInGroup(address, null);
  m.redraw();
}

/** Stop showing one address in the active view. The instance keeps running and stays in
 *  Everything and in any other project showing it. */
export function removeAddressFromView(address: string): void {
  const viewId = mountedViewId;
  if (viewId === null || isEverythingView(viewId)) return;
  dropPanelsForAddress(address, { keepPage: true });
  void removeProjectTab(viewId, address)
    .then(() => refreshProjectsList())
    .catch((e: Error) => {
      alert(`Failed to remove from project: ${e.message}`);
    })
    .finally(() => m.redraw());
}

/** Reload what a row is showing when it has an open tab; opens it fresh when it is backgrounded. */
export function refreshAddress(address: string): void {
  const panelId = panelIdForAddress(address);
  if (panelId === null) {
    openAddress(address);
    return;
  }
  refreshPanelContent(panelId);
}

/** Retitle an instance through its app, reporting a refusal. The ``apps_updated`` that
 *  follows the relay's refetch puts the new title on every surface. */
export function renameAddress(address: string, title: string): void {
  const parsed = parseAddress(address);
  if (parsed === null) return;
  void renameInstance(parsed.app, parsed.key, title)
    .then(() => syncTabTitlesFromInventory())
    .catch((e: Error) => {
      alert(`Could not rename to "${title}".\n\n${e.message}`);
    })
    .finally(() => m.redraw());
}

/** Reload whatever a tab is showing: every frame of the app, backgrounded ones included --
 *  "refresh the app" has always meant the app rather than this pane. */
function refreshPanelContent(panelId: string): void {
  const params = panelParams.get(panelId);
  if (params === undefined || params.kind === "launcher") return;
  const appName = appNameFromAddress(params.address);
  if (appName !== null) reloadIframesForApp(appName);
}

// ---------- Shortcuts ----------

/** Epoch milliseconds each open panel's address was last the active one, for the MRU rule. */
function lastFocusedMsByAddress(): Record<string, number> {
  const byAddress: Record<string, number> = {};
  for (const [panelId, params] of panelParams) {
    if (params.kind === "launcher") continue;
    const focused = lastFocusedMsByPanelId.get(panelId);
    if (focused !== undefined) byAddress[params.address] = focused;
  }
  return byAddress;
}

/**
 * The instance a focus-mode shortcut goes to among what the view lists: the most recently
 * focused open tab of the app in this client, else the app's own most recently active
 * instance the view lists, else the first listed. Null when the view lists none.
 */
export function mostRecentAddressOfApp(
  candidates: readonly { address: string; appName: string; lastActiveMs: number | null }[],
  appName: string,
  lastFocusedMs: Readonly<Record<string, number>>,
): string | null {
  const ofApp = candidates.filter((candidate) => candidate.appName === appName);
  if (ofApp.length === 0) return null;
  let best = ofApp[0];
  let bestScore = score(ofApp[0]);
  for (const candidate of ofApp.slice(1)) {
    const candidateScore = score(candidate);
    if (candidateScore > bestScore) {
      best = candidate;
      bestScore = candidateScore;
    }
  }
  return best.address;

  function score(candidate: { address: string; lastActiveMs: number | null }): number {
    const focused = lastFocusedMs[candidate.address];
    if (focused !== undefined) return focused;
    return candidate.lastActiveMs ?? Number.NEGATIVE_INFINITY;
  }
}

/** Go to the most recently used instance of ``appName`` the active view shows; false when it shows none. */
function focusExistingInstanceOf(appName: string): boolean {
  const candidates = getSidebarRows().map((row) => {
    const resolved = findInstance(row.address);
    const lastActive = resolved?.instance.last_active ?? null;
    return {
      address: row.address,
      appName: row.appName,
      lastActiveMs: lastActive === null ? null : Date.parse(lastActive),
    };
  });
  const address = mostRecentAddressOfApp(candidates, appName, lastFocusedMsByAddress());
  if (address === null) return false;
  openAddressInGroup(address, null);
  m.redraw();
  return true;
}

/** Actions in flight, as ``<app>:<action>`` keys: the surfaces that would run another stand down. */
const actionsAwaitingCreate = new Set<string>();

export function getAwaitingActionKeys(): ReadonlySet<string> {
  return actionsAwaitingCreate;
}

export function actionKey(appName: string, actionId: string): string {
  return `${appName}:${actionId}`;
}

/** Run one rail shortcut in its mode: focus goes to the view's most recent instance of the app
 *  (running the action only when it shows none), new always runs the action. */
export function runShortcut(shortcut: ProjectShortcut): void {
  const app = getApp(shortcut.app);
  if (app === undefined) return;
  if (shortcut.mode === "focus" && focusExistingInstanceOf(app.name)) return;
  void runActionInPane(app, shortcut.action, {}, null, null);
}

/** Always run the shortcut's action -- the menu's complementary "New X". */
export function runShortcutAsNew(shortcut: ProjectShortcut): void {
  const app = getApp(shortcut.app);
  if (app === undefined) return;
  void runActionInPane(app, shortcut.action, {}, null, null);
}

/** Focus the most recently used instance of the shortcut's app -- the menu's complementary
 *  action while the row is in new mode. Never creates. */
export function focusLastOfShortcut(shortcut: ProjectShortcut): void {
  focusExistingInstanceOf(shortcut.app);
}

/** Run an app's action from the launcher or the All apps popover: always creates. */
export function runAppAction(app: AppRecord, actionId: string, params: Record<string, string> = {}): void {
  void runActionInPane(app, actionId, params, null, null);
}

/**
 * Run one of an app's actions and dock what it made in ``targetGroup``.
 *
 * A single-instance app's ``open`` docks its one tab (or focuses it). Anything else goes
 * through the shell's relay, which asks the app to create an instance and refetches its list
 * before answering, so the record can be docked at once. One run per (app, action) at a time:
 * a create takes seconds, and a second click while it runs must not start a second instance.
 *
 * CLEANUP (phase 10 of the workspace app model): the chat app's ``new`` is special-cased --
 * the account the launcher's provider picker chose rides as ``account_id``, and with nothing
 * signed in the user is sent to the provider chooser first, then the chat starts on the
 * account they added.
 */
async function runActionInPane(
  app: AppRecord,
  actionId: string,
  params: Record<string, string>,
  targetGroup: DockviewGroupPanel | null,
  launcherPanelId: string | null,
): Promise<void> {
  if (!app.has_instances) {
    if (openAddressInGroup(addressFor(app.name, ""), targetGroup) !== null) retireLauncher(launcherPanelId);
    m.redraw();
    return;
  }
  const key = actionKey(app.name, actionId);
  if (actionsAwaitingCreate.has(key)) return;
  if (app.name === CHAT_APP_NAME && actionId === CHAT_NEW_ACTION) {
    if (!areAccountsLoaded()) await loadAccounts().catch(() => undefined);
    if ((params.account_id ?? "") === "" && getAccounts().length === 0) {
      openProviderChooser({
        onSignedIn: (accountId) => {
          void runActionInPane(app, actionId, { ...params, account_id: accountId }, targetGroup, launcherPanelId);
        },
      });
      return;
    }
  }
  actionsAwaitingCreate.add(key);
  if (launcherPanelId !== null) launchersAwaitingCreate.add(launcherPanelId);
  m.redraw();
  try {
    const record = await createInstance(app.name, actionId, params);
    const address = addressFor(app.name, record.key);
    if (!(await whenAddressListed(address))) {
      throw new Error(`${app.display_name} created ${record.key} but has not listed it`);
    }
    if (openAddressInGroup(address, targetGroup) !== null) retireLauncher(launcherPanelId);
  } catch (e) {
    alert(`Failed to open ${app.display_name}: ${(e as Error).message}`);
  } finally {
    actionsAwaitingCreate.delete(key);
    if (launcherPanelId !== null) launchersAwaitingCreate.delete(launcherPanelId);
    m.redraw();
  }
}

/** Add or re-mode one shortcut on the active project. */
export function setShortcutInView(appName: string, actionId: string, mode: ShortcutMode): void {
  const viewId = mountedViewId;
  if (viewId === null || isEverythingView(viewId)) return;
  void setProjectShortcut(viewId, appName, actionId, mode)
    .then(() => refreshProjectsList())
    .catch((e: Error) => {
      alert(`Failed to change the ${appName} shortcut: ${e.message}`);
    })
    .finally(() => m.redraw());
}

/** Take one shortcut off the active project's rail. */
export function removeShortcutFromView(appName: string, actionId: string): void {
  const viewId = mountedViewId;
  if (viewId === null || isEverythingView(viewId)) return;
  void removeProjectShortcut(viewId, appName, actionId)
    .then(() => refreshProjectsList())
    .catch((e: Error) => {
      alert(`Failed to unpin ${appName}: ${e.message}`);
    })
    .finally(() => m.redraw());
}

// ---------- Panels ----------

/** The panel currently showing ``address``, or null when the instance is backgrounded or gone. */
function panelIdForAddress(address: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.kind === "instance" && params.address === address) return panelId;
  }
  return null;
}

/** The panel whose tab record carries ``tabId``. The two ids coincide for panels this dock
 *  minted, but a layout the shell wrote (a migration) may name either, so the record decides. */
function panelIdForTabId(tabId: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.kind === "instance" && params.tabId === tabId) return panelId;
  }
  return null;
}

/** Any open panel of ``appName``, or null: what a bare app address resolves to for an agent. */
function anyPanelIdOfApp(appName: string): string | null {
  for (const [panelId, params] of panelParams) {
    if (params.kind === "instance" && appNameFromAddress(params.address) === appName) return panelId;
  }
  return null;
}

/** Position + size options passed through to ``dockview.addPanel``. */
type AddPanelPlacementOptions = {
  position?: { referenceGroup: string } | { referencePanel: string; direction: "left" | "right" | "above" | "below" };
  initialWidth?: number;
  initialHeight?: number;
};

/**
 * Dock ``address`` in a new panel with the supplied placement, filing it into the active
 * project's tab set. The instance must be listed: a caller that just created it awaits
 * ``whenAddressListed`` first.
 */
function addPanelForAddress(address: string, placement: AddPanelPlacementOptions): string | null {
  if (!dockview) return null;
  const resolved = findInstance(address);
  if (resolved === null) {
    console.warn(`[si] cannot open ${address}: no app lists it`);
    return null;
  }
  const tabId = mintTabId();
  const params: PanelParams = { kind: "instance", address, tabId };
  panelParams.set(tabId, params);
  dockview.addPanel({
    id: tabId,
    component: INSTANCE_COMPONENT,
    title: resolved.instance.title,
    params,
    ...placement,
  });
  fileIntoActiveProject(address);
  return tabId;
}

/**
 * File a freshly-opened address into the view it was opened in (the uniform rule: every open
 * in a project files, subagents included). Idempotent, and Everything takes no tab set.
 * Best-effort: failing to reach the shell must never stop a tab from opening.
 */
function fileIntoActiveProject(address: string): void {
  const viewId = mountedViewId;
  if (viewId === null || isEverythingView(viewId)) return;
  if (projectForViewId(availableProjects, viewId)?.tabs.includes(address) === true) return;
  void addProjectTab(viewId, address)
    .then(() => refreshProjectsList())
    .catch((e: Error) => {
      // The tab is open regardless, and opening it again files it again.
      console.warn(`[si] could not file ${address} into ${viewId}`, e);
    });
}

/** Drop every panel showing ``address`` from the live dock. The page goes too unless the
 *  caller says otherwise (a remove-from-project keeps the instance and its page). */
function dropPanelsForAddress(address: string, options: { keepPage?: boolean } = {}): void {
  if (!options.keepPage) destroyLiveSurface(address);
  if (!dockview) return;
  for (const panel of [...dockview.panels]) {
    const params = panelParams.get(panel.id);
    if (params?.kind === "instance" && params.address === address) dockview.removePanel(panel);
  }
}

/**
 * Change which address a tab shows: the panel keeps its id and its page, which is re-filed
 * under the new address. What a tab_rebound push (or a save-time reconcile) does.
 */
function rebindPanel(panelId: string, address: string): void {
  const params = panelParams.get(panelId);
  if (params === undefined || params.kind === "launcher" || params.address === address) return;
  // One page per instance: a tab already showing the target closes (the rebound pane is where
  // the user acted), and its page goes with it rather than lingering unfiled.
  dropPanelsForAddress(address);
  rekeyLiveSurface(params.address, address);
  params.address = address;
  syncTabTitlesFromInventory();
  fileIntoActiveProject(address);
  m.redraw();
}

function buildLayoutPayload(): { dockview: SerializedDockview; tabs: Record<string, TabRecord> } | null {
  if (!dockview) return null;
  const tabs: Record<string, TabRecord> = {};
  for (const [panelId, params] of panelParams) {
    if (params.kind === "launcher") continue;
    tabs[panelId] = {
      address: params.address,
      tab_id: params.tabId,
      last_focused_ms: lastFocusedMsByPanelId.get(panelId) ?? 0,
    };
  }
  return { dockview: dockview.toJSON(), tabs };
}

async function persistLayout(): Promise<void> {
  if (!dockview || isLayoutSaveSuspended || settledViewGeneration !== viewMountGeneration) return;
  const targetViewId = mountedViewId;
  if (targetViewId === null) return;
  const payload = buildLayoutPayload();
  if (payload === null) return;
  const serialized = JSON.stringify(payload);
  if (serialized === lastPersistedLayoutJson) return;
  try {
    await saveLayout(targetViewId, getClientId(), payload.dockview, payload.tabs);
    lastPersistedLayoutJson = serialized;
  } catch (e) {
    // Best-effort (the project was deleted mid-flight, say; the push switches us to the fallback).
    console.warn(`[si] could not save the layout of ${targetViewId}`, e);
  }
}

function scheduleSave(): void {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
  }
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void persistLayout();
  }, AUTOSAVE_DEBOUNCE_MS);
}

/** Flush a pending debounced autosave now, so edits made just before a switch land in the
 *  layout they were made in. */
async function flushPendingSave(): Promise<void> {
  if (saveTimer !== null) {
    clearTimeout(saveTimer);
    saveTimer = null;
    await persistLayout();
  }
}

/** Re-list the projects after a write of ours; the projects-updated listener takes the list. A
 *  listing the shell could not answer changes nothing: the push that follows the write will. */
async function refreshProjectsList(): Promise<void> {
  const listed = await fetchProjectsList();
  if (listed === null) return;
  applyProjects(listed);
}

/** Take the shell's project list; a client whose mounted project is gone (deleted here or
 *  elsewhere) moves to the view it would land on afresh. */
function takeProjects(projects: ProjectInfo[]): void {
  availableProjects = projects;
  const viewId = mountedViewId;
  if (viewId !== null && !isEverythingView(viewId) && projectForViewId(projects, viewId) === null) {
    void switchToView(chooseInitialViewId(projects, viewId));
  }
  m.redraw();
}

/**
 * Mount a layout into the dockview, replacing whatever is currently shown.
 *
 * A layout with no dockview (never arranged, or nothing could be fetched) mounts the New Tab
 * launcher. Panels whose address the machine no longer lists are dropped before the restore:
 * that observation is what prunes references, and the shell's own file already lost them.
 */
async function applyLayout(
  layout: { dockview: SerializedDockview | null; tabs: Record<string, TabRecord> } | null,
  generation: number,
): Promise<void> {
  if (!dockview) return;
  // Every page url is derived from its app's origin label, which only resolves once the app
  // list has loaded; bounded, so a workspace that reports no apps still proceeds.
  const isInventoryKnown = await whenAppsLoaded();
  if (!dockview || generation !== viewMountGeneration) return;
  const dv = dockview;
  isApplyingLayout = true;

  // Tear the outgoing layout down BEFORE seeding the incoming params. Only the panels go: the
  // pages stay mounted and hidden until an incoming slot picks them up again.
  dv.clear();
  panelParams.clear();
  lastFocusedMsByPanelId.clear();

  if (layout !== null && layout.dockview !== null) {
    // Nothing is unlisted until the inventory has arrived: an empty seed is not an answer, and
    // the apps_updated that brings the list prunes then (contracts section 8).
    const unlisted = new Set(
      isInventoryKnown ? panelsWithUnlistedAddresses(layout.tabs, (address) => !isAddressUnlisted(address)) : [],
    );
    for (const [panelId, tab] of Object.entries(layout.tabs)) {
      if (unlisted.has(panelId)) continue;
      panelParams.set(panelId, { kind: "instance", address: tab.address, tabId: tab.tab_id });
      lastFocusedMsByPanelId.set(panelId, tab.last_focused_ms);
    }
    panelsPrunedByRestore = unlisted;
    try {
      dv.fromJSON(layout.dockview);
    } catch (e) {
      console.warn(`[si] could not restore the saved arrangement of ${mountedViewId ?? "?"}; starting it over`, e);
      panelParams.clear();
      dv.clear();
    }
    for (const panel of dv.panels.slice()) {
      if (unlisted.has(panel.id)) dv.removePanel(panel);
    }
    panelsPrunedByRestore = new Set();
    // An instance is a singleton with one page, so an arrangement naming the same one twice
    // would give two tabs a page to fight over. The first occurrence keeps it.
    for (const duplicatePanelId of duplicateLiveKeyPanelIds(
      dv.panels.map((panel) => ({ panelId: panel.id, key: liveKeyForPanel(panelParams.get(panel.id)) })),
    )) {
      const panel = dv.panels.find((candidate) => candidate.id === duplicatePanelId);
      if (panel) dv.removePanel(panel);
    }
  }

  if (dv.panels.length === 0) {
    openLauncherPanel(null);
  }
  isApplyingLayout = false;
  settledViewGeneration = generation;
  syncTabTitlesFromInventory();
  reconcileLiveSurfaces();
  scheduleTabWidthRecompute();
}

function setActiveView(viewId: string): void {
  setActiveProjectId(viewId);
  mountedViewId = viewId;
}

/** Pick this client's initial view, register it with the shell, and mount its arrangement. */
async function initializeActiveView(): Promise<void> {
  const generation = ++viewMountGeneration;
  const listed = await fetchProjectsList();
  if (generation !== viewMountGeneration) return;
  // A listing the shell could not answer changes nothing: the push on connect, which may
  // already have landed, is the list.
  if (listed !== null) availableProjects = listed;
  applyProjects(availableProjects);
  const chosenId = chooseInitialViewId(availableProjects, getStoredProjectId());
  setActiveView(chosenId);
  reportClientState();
  const layout = await fetchLayoutOrSuspendSaves(chosenId);
  if (generation !== viewMountGeneration) return;
  lastPersistedLayoutJson = null;
  await applyLayout(layout, generation);
  m.redraw();
}

/** This client's layout of ``viewId``; when the shell cannot answer, the launcher shows and
 *  autosave stays off until a fetch succeeds, so nothing is saved over the real arrangement. */
async function fetchLayoutOrSuspendSaves(viewId: string): Promise<LayoutRecord | null> {
  try {
    const layout = await fetchLayout(viewId, getClientId());
    isLayoutSaveSuspended = false;
    return layout;
  } catch (e) {
    console.warn(`[si] could not fetch the layout of ${viewId}; autosave is off until it loads`, e);
    isLayoutSaveSuspended = true;
    return null;
  }
}

/**
 * Switch this client onto another view: flush pending edits into the old one, repoint the
 * autosave target, tell the shell (which records the switch), and mount the new arrangement.
 */
export async function switchToView(viewId: string): Promise<void> {
  if (!dockview) return;
  const previousViewId = mountedViewId ?? getActiveProjectId();
  if (previousViewId === viewId) return;
  const generation = ++viewMountGeneration;
  await flushPendingSave();
  if (generation !== viewMountGeneration) return;
  setActiveView(viewId);
  reportClientState(previousViewId);
  const layout = await fetchLayoutOrSuspendSaves(viewId);
  if (generation !== viewMountGeneration) return;
  lastPersistedLayoutJson = null;
  await applyLayout(layout, generation);
  m.redraw();
}

function soleLauncherPanelId(): string | null {
  if (!dockview || dockview.panels.length !== 1) return null;
  const panelId = dockview.panels[0].id;
  return panelParams.get(panelId)?.kind === "launcher" ? panelId : null;
}

/**
 * Start the chat a freshly-created project is made with. Every project starts with one chat,
 * so the user lands in a working chat instead of an empty launcher. The create runs only if
 * that project is still the mounted one, since the open files into the mounted view.
 */
export async function startProjectChat(projectId: string): Promise<void> {
  if (mountedViewId !== projectId) return;
  const chatApp = getApp(CHAT_APP_NAME);
  if (chatApp === undefined) return;
  const launcherPanelId = soleLauncherPanelId();
  await runActionInPane(chatApp, CHAT_NEW_ACTION, {}, null, launcherPanelId);
}

/** Open a new chat on ``accountId``, wherever the user currently is. */
export async function startChatOnAccount(accountId: string): Promise<void> {
  const chatApp = getApp(CHAT_APP_NAME);
  if (chatApp === undefined) return;
  await runActionInPane(chatApp, CHAT_NEW_ACTION, { account_id: accountId }, null, null);
}

/** Put the title each instance has onto the tab showing it, from the inventory. */
function syncTabTitlesFromInventory(): void {
  if (!dockview) return;
  for (const panel of dockview.panels) {
    const resolved = resolvedInstanceForPanel(panel.id);
    if (resolved === null) continue;
    if (resolved.instance.title !== "" && resolved.instance.title !== panel.title) {
      panel.api.setTitle(resolved.instance.title);
    }
  }
}

/** The inventory moved: drop the panels whose addresses left it, and re-sync the titles. */
function reconcilePanelsWithInventory(): void {
  if (!dockview) return;
  for (const panel of [...dockview.panels]) {
    const params = panelParams.get(panel.id);
    if (params === undefined || params.kind === "launcher") continue;
    if (!isAddressUnlisted(params.address)) continue;
    destroyLiveSurface(params.address);
    dockview.removePanel(panel);
  }
  // Pages of instances that are gone but were not docked in this view go too.
  for (const key of liveSurfaceKeys()) {
    if (isAddressUnlisted(key)) destroyLiveSurface(key);
  }
  syncTabTitlesFromInventory();
}

// ---------- The app contract's shell side ----------

/** The panel a child frame stands in for. Null for a frame the dock is not showing. */
function panelForChildFrame(frame: HTMLIFrameElement): IDockviewPanel | null {
  const address = frame.getAttribute(IFRAME_PANEL_ADDRESS_ATTR);
  if (address === null) return null;
  const panelId = liveSurfaceBoundPanelId(address);
  if (panelId === null) return null;
  return dockview?.panels.find((panel) => panel.id === panelId) ?? null;
}

/** ``shell:focused`` from a page: the pane showing it becomes the active one. */
function activatePanelForChildFrame(frame: HTMLIFrameElement): void {
  const panel = panelForChildFrame(frame);
  if (panel !== null && !panel.api.isActive) dockview?.setActivePanel(panel);
}

/** ``shell:open`` from a page: dock an instance of the posting frame's own app beside it, or
 *  focus the tab already showing it. An address the inventory does not list yet (a create the
 *  page ran itself) is awaited briefly. */
function openInstanceForChildFrame(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
  const address = payload.address;
  if (typeof address !== "string") return;
  const postingApp = frame.getAttribute(IFRAME_PANEL_APP_ATTR);
  if (postingApp === null || appNameFromAddress(address) !== postingApp) {
    console.warn(
      `[si] shell:open ignored: ${address} does not name the posting frame's app (${postingApp ?? "none"})`,
    );
    return;
  }
  const targetGroup = panelForChildFrame(frame)?.api.group ?? null;
  void openAddressWhenListed(address, targetGroup);
}

/** Resolve to whether the inventory lists ``address``: at once, when its next list arrives, or
 *  false once ``AWAIT_ADDRESS_TIMEOUT_MS`` passes without it. */
function whenAddressListed(address: string): Promise<boolean> {
  if (findInstance(address) !== null) return Promise.resolve(true);
  return new Promise<boolean>((resolve) => {
    const settle = (isListed: boolean): void => {
      removeAppsUpdatedListener(listener);
      clearTimeout(timer);
      resolve(isListed);
    };
    const listener = (): void => {
      if (findInstance(address) !== null) settle(true);
    };
    const timer = setTimeout(() => settle(false), AWAIT_ADDRESS_TIMEOUT_MS);
    addAppsUpdatedListener(listener);
  });
}

/** Dock ``address`` once the inventory lists it. */
async function openAddressWhenListed(address: string, targetGroup: DockviewGroupPanel | null): Promise<void> {
  if (!(await whenAddressListed(address))) {
    console.warn(`[si] cannot open ${address}: its app has not listed it`);
    return;
  }
  openAddressInGroup(address, targetGroup);
  m.redraw();
}

/** ``shell:location`` from a page: relayed to the app that owns the instance. */
function relayLocationForChildFrame(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
  const address = frame.getAttribute(IFRAME_PANEL_ADDRESS_ATTR);
  const path = payload.path;
  if (address === null || typeof path !== "string" || path === "") return;
  const parsed = parseAddress(address);
  if (parsed === null || parsed.key === "") return;
  // Remembered before the relay: the record catching up with the page is not a reload.
  recordReportedPath(address, path);
  void reportInstanceLocation(parsed.app, parsed.key, path);
}

// ---------- Agent-driven layout op handlers ----------

function directionToPosition(direction: string): "top" | "bottom" | "left" | "right" {
  switch (direction) {
    case "above":
      return "top";
    case "below":
      return "bottom";
    case "left":
      return "left";
    default:
      return "right";
  }
}

/** Resolve a layout-op address (or the literal "self") to a live dockview panel id, or null. */
function resolveAddressToPanelId(address: string, requesterAgentId: string): string | null {
  if (!dockview) return null;
  if (address === "self") {
    if (!requesterAgentId) return null;
    return panelIdForAddress(addressFor(CHAT_APP_NAME, requesterAgentId));
  }
  const parsed = parseAddress(address);
  if (parsed === null) return null;
  const exact = panelIdForAddress(address);
  if (exact !== null) return exact;
  // A bare app address is satisfied by any open instance of the app, the way layout.py reads it.
  return parsed.key === "" ? anyPanelIdOfApp(parsed.app) : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

async function handleLayoutOp(event: LayoutOpEvent): Promise<void> {
  if (!dockview) return;
  const requesterAgentId = event.requesterAgentId;
  switch (event.op) {
    case "open":
      await handleOpen(event.args, requesterAgentId);
      return;
    case "focus":
      handleFocus(event.args, requesterAgentId);
      return;
    case "split":
      await handleSplit(event.args, requesterAgentId);
      return;
    case "close":
      handleClose(event.args, requesterAgentId);
      return;
    case "move":
      handleMove(event.args, requesterAgentId);
      return;
    case "maximize":
      handleMaximize(event.args, requesterAgentId);
      return;
    case "restore":
      handleRestore();
      return;
    case "refresh":
      handleRefresh(event.args, requesterAgentId);
      return;
    case "reload_system_interface":
      reloadInterface();
      return;
  }
}

/** Find a group adjacent to ``anchorGroupId`` in the requested direction, measured
 *  geometrically; among candidates the one with the largest perpendicular overlap wins. */
function findSiblingGroupInDirection(
  anchorGroupId: string,
  direction: "left" | "right" | "above" | "below",
): { id: string } | null {
  if (!dockview) return null;
  const anchor = dockview.groups.find((g) => g.id === anchorGroupId);
  if (!anchor) return null;
  const anchorRect = anchor.element.getBoundingClientRect();
  const tolerance = 2;
  let best: { id: string; overlap: number; distance: number } | null = null;
  for (const group of dockview.groups) {
    if (group.id === anchorGroupId) continue;
    const rect = group.element.getBoundingClientRect();
    let inDirection: boolean;
    let overlap: number;
    let distance: number;
    if (direction === "right") {
      inDirection = rect.left >= anchorRect.right - tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = rect.left - anchorRect.right;
    } else if (direction === "left") {
      inDirection = rect.right <= anchorRect.left + tolerance;
      overlap = Math.max(0, Math.min(rect.bottom, anchorRect.bottom) - Math.max(rect.top, anchorRect.top));
      distance = anchorRect.left - rect.right;
    } else if (direction === "below") {
      inDirection = rect.top >= anchorRect.bottom - tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = rect.top - anchorRect.bottom;
    } else {
      inDirection = rect.bottom <= anchorRect.top + tolerance;
      overlap = Math.max(0, Math.min(rect.right, anchorRect.right) - Math.max(rect.left, anchorRect.left));
      distance = anchorRect.top - rect.bottom;
    }
    if (!inDirection || overlap <= 0) continue;
    if (best === null || overlap > best.overlap || (overlap === best.overlap && distance < best.distance)) {
      best = { id: group.id, overlap, distance };
    }
  }
  return best === null ? null : { id: best.id };
}

/** The placement an agent's ``open`` docks into: to the right of the requester's own chat when
 *  it is docked, else of whatever the user is looking at, tabbing into a group already there
 *  unless ``forceNewGroup``. */
function placementForAgentOpen(requesterAgentId: string, forceNewGroup: boolean): AddPanelPlacementOptions {
  if (!dockview) return {};
  const anchorPanelId = resolveAddressToPanelId("self", requesterAgentId) ?? dockview.activePanel?.id ?? null;
  if (anchorPanelId === null) return {};
  const anchorGroupId = dockview.panels.find((p) => p.id === anchorPanelId)?.api.group.id ?? null;
  const sibling =
    !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, "right") : null;
  if (sibling !== null) return { position: { referenceGroup: sibling.id } };
  const containerWidth = dockviewContainer?.getBoundingClientRect().width ?? 0;
  const initialWidth = containerWidth > 0 ? Math.round(containerWidth * OPEN_TAB_SPLIT_FRACTION) : undefined;
  return { position: { referencePanel: anchorPanelId, direction: "right" }, initialWidth };
}

/** Dock an address with ``placement``: an open tab is focused; a listed instance is docked; a
 *  bare address of an app with instances runs the app's primary action. */
async function openForAgent(address: string, placement: AddPanelPlacementOptions): Promise<void> {
  if (!dockview) return;
  const parsed = parseAddress(address);
  if (parsed === null) return;
  const app = getApp(parsed.app);
  if (app === undefined) return;
  if (parsed.key === "" && app.has_instances) {
    const action = primaryActionForApp(app);
    if (action === null) return;
    const key = actionKey(app.name, action.id);
    if (actionsAwaitingCreate.has(key)) return;
    actionsAwaitingCreate.add(key);
    m.redraw();
    try {
      const record = await createInstance(app.name, action.id, {});
      const address = addressFor(app.name, record.key);
      if (!(await whenAddressListed(address))) {
        console.warn(`[si] ${app.name} created ${record.key} for an agent but has not listed it`);
        return;
      }
      addPanelForAddress(address, placement);
    } catch (e) {
      console.warn(`[si] could not run ${action.id} on ${app.name} for an agent: ${(e as Error).message}`);
    } finally {
      actionsAwaitingCreate.delete(key);
      m.redraw();
    }
    return;
  }
  if (findInstance(address) === null) return;
  addPanelForAddress(address, placement);
  m.redraw();
}

/**
 * Focus the panel already showing ``address`` (any instance of the app, for a bare address),
 * or report that none does. A bare app address for an app with instances always creates (a
 * fresh terminal each ``open terminal``), so it never counts as docked.
 */
function focusDockedPanel(address: string, requesterAgentId: string): boolean {
  if (!dockview) return false;
  const parsed = parseAddress(address);
  const isCreating = parsed !== null && parsed.key === "" && getApp(parsed.app)?.has_instances === true;
  if (isCreating) return false;
  const existing = resolveAddressToPanelId(address, requesterAgentId);
  if (existing === null) return false;
  const panel = dockview.panels.find((p) => p.id === existing);
  if (panel) dockview.setActivePanel(panel);
  return true;
}

async function handleOpen(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  const address = asString(args.address);
  if (!address || !dockview) return;
  if (focusDockedPanel(address, requesterAgentId)) return;
  await openForAgent(address, placementForAgentOpen(requesterAgentId, args.new_group === true));
}

function handleFocus(args: Record<string, unknown>, requesterAgentId: string): void {
  if (!dockview) return;
  const address = asString(args.address);
  if (!address) return;
  const panelId = resolveAddressToPanelId(address, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.setActivePanel(panel);
}

function directionFromArg(direction: string): "left" | "right" | "above" | "below" {
  if (direction === "left" || direction === "right" || direction === "above" || direction === "below") {
    return direction;
  }
  return "right";
}

function isWithinDirection(direction: string): boolean {
  return direction === "within";
}

function computeInitialSize(
  direction: string,
  ratio: number | null,
  containerRect: DOMRect | undefined,
): { initialWidth?: number; initialHeight?: number } {
  if (ratio === null || !containerRect) return {};
  if (direction === "above" || direction === "below") {
    const h = containerRect.height > 0 ? Math.round(containerRect.height * ratio) : undefined;
    return h ? { initialHeight: h } : {};
  }
  const w = containerRect.width > 0 ? Math.round(containerRect.width * ratio) : undefined;
  return w ? { initialWidth: w } : {};
}

async function handleSplit(args: Record<string, unknown>, requesterAgentId: string): Promise<void> {
  if (!dockview) return;
  const address = asString(args.address);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction) ?? "right";
  const ratio = asNumber(args.ratio);
  const forceNewGroup = args.new_group === true;
  if (!address || !relativeTo) return;
  // An instance is one page: a second panel for it would take the page from the first.
  if (focusDockedPanel(address, requesterAgentId)) return;
  // ``relative_to=self`` strictly anchors against the requester's own chat panel.
  const referencePanelId = resolveAddressToPanelId(relativeTo, requesterAgentId);
  if (referencePanelId === null) return;
  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  const anchorGroupId = referencePanel?.api.group.id ?? null;
  if (isWithinDirection(direction)) {
    if (anchorGroupId === null) return;
    await openForAgent(address, { position: { referenceGroup: anchorGroupId } });
    return;
  }
  const directionArg = directionFromArg(direction);
  const sibling =
    !forceNewGroup && anchorGroupId !== null ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  const sizes = computeInitialSize(direction, ratio, dockviewContainer?.getBoundingClientRect());
  const placement: AddPanelPlacementOptions =
    sibling !== null
      ? { position: { referenceGroup: sibling.id } }
      : { position: { referencePanel: referencePanelId, direction: directionArg }, ...sizes };
  await openForAgent(address, placement);
}

function handleClose(args: Record<string, unknown>, requesterAgentId: string): void {
  if (!dockview) return;
  const address = asString(args.address);
  if (!address) return;
  const panelId = resolveAddressToPanelId(address, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) dockview.removePanel(panel);
}

function handleMove(args: Record<string, unknown>, requesterAgentId: string): void {
  if (!dockview) return;
  const address = asString(args.address);
  const relativeTo = asString(args.relative_to);
  const direction = asString(args.direction);
  const forceNewGroup = args.new_group === true;
  if (!address || !relativeTo || !direction) return;
  const targetPanelId = resolveAddressToPanelId(address, requesterAgentId);
  const referencePanelId = resolveAddressToPanelId(relativeTo, requesterAgentId);
  if (targetPanelId === null || referencePanelId === null) return;
  const targetPanel = dockview.panels.find((p) => p.id === targetPanelId);
  const referencePanel = dockview.panels.find((p) => p.id === referencePanelId);
  if (!targetPanel || !referencePanel) return;
  const anchorGroupId = referencePanel.api.group.id;
  if (isWithinDirection(direction)) {
    // A sole-occupant panel moved into its own group would be disposed with it; no-op instead.
    if (targetPanel.api.group.id === referencePanel.api.group.id) return;
    targetPanel.api.moveTo({ group: referencePanel.api.group });
    return;
  }
  const directionArg = directionFromArg(direction);
  const sibling = !forceNewGroup ? findSiblingGroupInDirection(anchorGroupId, directionArg) : null;
  if (sibling !== null) {
    const siblingGroup = dockview.groups.find((g) => g.id === sibling.id);
    if (siblingGroup) {
      if (siblingGroup.id === targetPanel.api.group.id) return;
      targetPanel.api.moveTo({ group: siblingGroup });
      return;
    }
  }
  targetPanel.api.moveTo({
    group: referencePanel.api.group,
    position: directionToPosition(direction),
  });
}

function handleMaximize(args: Record<string, unknown>, requesterAgentId: string): void {
  if (!dockview) return;
  const address = asString(args.address);
  if (!address) return;
  const panelId = resolveAddressToPanelId(address, requesterAgentId);
  if (panelId === null) return;
  const panel = dockview.panels.find((p) => p.id === panelId);
  if (panel) panel.api.maximize();
}

function handleRestore(): void {
  if (!dockview) return;
  for (const panel of dockview.panels) {
    if (panel.api.isMaximized()) {
      panel.api.exitMaximized();
      return;
    }
  }
}

/** ``refresh app:<name>`` reloads every frame of the app; an instance address reloads one. */
function handleRefresh(args: Record<string, unknown>, requesterAgentId: string): void {
  const address = asString(args.address);
  if (!address) return;
  // ``self`` with no requester would read as the bare chat app and reload every chat.
  if (address === "self" && !requesterAgentId) return;
  const target = address === "self" ? addressFor(CHAT_APP_NAME, requesterAgentId) : address;
  const parsed = parseAddress(target);
  if (parsed === null) return;
  if (parsed.key === "" && getApp(parsed.app)?.has_instances === true) {
    reloadIframesForApp(parsed.app);
    return;
  }
  reloadIframeForAddress(target);
}

// ---------- Live pages ----------

function mountLiveContent(surface: LiveSurface): void {
  m.mount(surface.element, { view: () => renderLiveContent(surface) });
}

function renderLiveContent(surface: LiveSurface): m.Children {
  const params = surface.params;
  if (params.kind === "launcher") return null;
  const resolved = findInstance(params.address);
  if (resolved === null) {
    // A stopped app's list is never fetched, so its addresses resolve to nothing until it
    // runs again; the pane still says the app is stopped and offers the Start.
    const appName = appNameFromAddress(params.address);
    const app = appName === null ? undefined : getApp(appName);
    const stopped = app === undefined ? null : stoppedPlaceholderForApp(app);
    if (app !== undefined && stopped !== null) {
      return m(StoppedAppPlaceholder, { ...stopped, appName: app.name });
    }
    const note = isAddressUnlisted(params.address)
      ? "This tab's app no longer lists it."
      : "Waiting for this tab's app to list it.";
    return m(
      "div",
      { class: "dockview-panel-unrecoverable flex h-full items-center justify-center p-4 text-center" },
      [note],
    );
  }
  const { app, instance } = resolved;
  const url = instancePageUrl(app, instance, params.tabId);
  return m(IframePanel, {
    url,
    isPageAtUrl: isPageAtListedUrl(url, surface.lastReportedPath),
    onNavigate: () => clearReportedPath(surface.key),
    title: instance.title,
    appName: app.name,
    address: params.address,
    stopped: stoppedPlaceholderForApp(app),
    contract: {
      address: params.address,
      tabId: params.tabId,
      viewId: getActiveProjectId(),
      isVisible: surface.isVisible,
    },
  });
}

/**
 * A dockview panel, now that a panel is only a place: an empty div dockview creates,
 * positions, hides and disposes at will, standing in for a live page that outlives it.
 */
function createLiveSlotRenderer(
  panelId: string,
  params: Extract<PanelParams, { kind: "instance" }>,
): IContentRenderer {
  const element = document.createElement("div");
  element.className = "si-live-slot";
  return {
    element,
    init(parameters) {
      const surface = ensureLiveSurface(params.address, params, mountLiveContent);
      // What is on screen is what the next autosave has to record, so this view's bookkeeping
      // entry becomes the very params object the page renders from.
      panelParams.set(panelId, surface.params);
      bindSlot(surface, panelId, parameters.api);
    },
    dispose() {
      unbindSlot(panelId);
    },
  };
}

function closeActiveTabFromEmbedder(): void {
  const activePanel = dockview?.activePanel;
  if (!activePanel) return;
  const key = liveKeyForPanel(panelParams.get(activePanel.id));
  const frame = key === null ? null : (liveSurfaceElement(key)?.querySelector("iframe") ?? null);
  if (frame !== null) sendToChildFrame(frame, SHELL_CLOSE_REQUEST);
  activePanel.api.close();
}

function createPlaceholderPanelRenderer(text: string): IContentRenderer {
  const element = document.createElement("div");
  element.className = "dockview-panel-unrecoverable";
  element.style.display = "flex";
  element.style.alignItems = "center";
  element.style.justifyContent = "center";
  element.style.height = "100%";
  element.style.padding = "16px";
  element.style.textAlign = "center";
  element.textContent = text;
  return {
    element,
    init() {},
    dispose() {},
  };
}

function createUnrecoverablePanelRenderer(panelId: string): IContentRenderer {
  console.warn(`Rendering unrecoverable-panel placeholder for dockview panel ${panelId}`);
  return createPlaceholderPanelRenderer(
    "This tab's contents could not be restored. Close it and open it again from the sidebar.",
  );
}

function initializeDockview(parentElement: HTMLElement): void {
  if (initialized) return;
  initialized = true;

  dockviewContainer = document.createElement("div");
  dockviewContainer.className = "dockview-agent-container dockview-theme-light";
  dockviewContainer.style.width = "100%";
  dockviewContainer.style.height = "100%";
  dockviewContainer.style.position = "relative";
  parentElement.appendChild(dockviewContainer);

  tabStripObserver = new ResizeObserver(() => {
    scheduleTabWidthRecompute();
  });
  window.addEventListener("resize", scheduleTabWidthRecompute);
  window.addEventListener("resize", scheduleReconcile);

  // dockview-core's Scrollbar only reads event.deltaY; translate a horizontal wheel into the
  // tab strip's scrollLeft.
  dockviewContainer.addEventListener(
    "wheel",
    (event: WheelEvent) => {
      if (event.deltaX === 0) return;
      const target = event.target;
      if (!(target instanceof Element)) return;
      const tabsContainer = target.closest<HTMLElement>(".dv-tabs-container");
      if (!tabsContainer || !dockviewContainer?.contains(tabsContainer)) return;
      event.preventDefault();
      tabsContainer.scrollLeft += event.deltaX;
    },
    { passive: false },
  );

  const dv = new DockviewComponent(dockviewContainer, {
    theme: themeLight,
    defaultRenderer: "always",
    defaultTabComponent: "custom",
    createComponent(options) {
      // dockview supplies ``params`` for panels created through ``addPanel`` but not for panels
      // it recreates from ``fromJSON``; those fall back to the entry the restore seeded.
      const suppliedParams = (options as unknown as { params?: PanelParams }).params;
      const params = suppliedParams ?? panelParams.get(options.id);
      if (params === undefined) {
        if (options.name === LAUNCHER_COMPONENT || options.id.startsWith(LAUNCHER_PANEL_ID_PREFIX)) {
          panelParams.set(options.id, { kind: "launcher" });
          return createLauncherRenderer(options.id);
        }
        if (panelsPrunedByRestore.has(options.id)) {
          return createPlaceholderPanelRenderer("This tab's app no longer lists it.");
        }
        return createUnrecoverablePanelRenderer(options.id);
      }
      if (params.kind === "launcher") {
        panelParams.set(options.id, params);
        return createLauncherRenderer(options.id);
      }
      panelParams.set(options.id, params);
      return createLiveSlotRenderer(options.id, params);
    },
    createTabComponent(options) {
      return createCustomTab(options);
    },
    createRightHeaderActionComponent(group) {
      return createAddTabButton(group);
    },
  });

  dockview = dv;

  initializeLiveLayer(dv.overlayRenderContainer.element, redrawForVisibility);

  const endDrag = (): void => {
    setDragInProgress(false);
  };
  dv.api.onWillDragPanel(() => {
    setDragInProgress(true);
  });
  dv.api.onWillDragGroup(() => {
    setDragInProgress(true);
  });
  dv.api.onDidDrop(endDrag);
  document.addEventListener("dragend", endDrag, true);
  document.addEventListener("drop", endDrag, true);
  document.addEventListener("pointerdown", endDrag, true);

  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, closeActiveTabFromEmbedder);

  // The shell side of the app contract (contracts.md section 10).
  setChildFrameMessageHandler(SHELL_FOCUSED, activatePanelForChildFrame);
  setChildFrameMessageHandler(SHELL_OPEN, openInstanceForChildFrame);
  setChildFrameMessageHandler(SHELL_LOCATION, relayLocationForChildFrame);

  dv.api.onDidLayoutChange(() => {
    scheduleSave();
    scheduleTabWidthRecompute();
    scheduleReconcile();
  });
  dv.api.onDidMovePanel(() => {
    scheduleReconcile();
  });
  dv.api.onDidMaximizedGroupChange(() => {
    scheduleReconcile();
  });

  // Closing a tab is nothing more than that: the instance keeps running, its page stays
  // mounted and hidden, and it stays in every tab set holding it.
  dv.api.onDidRemovePanel((panel) => {
    panelParams.delete(panel.id);
    lastFocusedMsByPanelId.delete(panel.id);
    ensureDockIsNotEmpty();
    scheduleTabWidthRecompute();
  });
  dv.api.onDidAddPanel(() => {
    scheduleTabWidthRecompute();
  });

  dv.api.onDidActivePanelChange((panel) => {
    if (panel === undefined || isApplyingLayout) return;
    if (panelParams.get(panel.id)?.kind === "instance") {
      lastFocusedMsByPanelId.set(panel.id, Date.now());
      scheduleSave();
    }
    retireLaunchersOnFocusLeaving(panel.id);
    focusFrameOfActivatedPanel(panel.id);
  });

  // The inventory moved: titles, statuses, and which instances still exist.
  addAppsUpdatedListener(() => {
    reconcilePanelsWithInventory();
    m.redraw();
  });

  addProjectsUpdatedListener(takeProjects);

  addLayoutOpListener((event: LayoutOpEvent) => {
    void handleLayoutOp(event);
  });

  addLoadViewListener((event) => {
    if (event.targetClientId !== null && event.targetClientId !== getClientId()) return;
    if (event.viewId === mountedViewId) return;
    void switchToView(event.viewId);
  });

  addTabReboundListener((event: TabReboundEvent) => {
    if (event.clientId !== getClientId() || event.viewId !== mountedViewId) return;
    const panelId = panelIdForTabId(event.tabId);
    if (panelId !== null) rebindPanel(panelId, event.address);
  });

  void initializeActiveView();
}

export const DockviewWorkspace: m.Component = {
  oncreate(vnode: m.VnodeDOM) {
    const wrapper = vnode.dom as HTMLElement;
    initializeDockview(wrapper);
  },

  onupdate(_vnode: m.VnodeDOM) {
    if (dockview && dockviewContainer) {
      requestAnimationFrame(() => {
        if (dockviewContainer) {
          const rect = dockviewContainer.getBoundingClientRect();
          dockview!.layout(rect.width, rect.height);
        }
      });
    }
  },

  view() {
    return m(
      "div",
      {
        class: "dockview-workspace",
        style: "width: 100%; height: 100%;",
      },
      [
        deleteDialog !== null
          ? m(DestroyConfirmDialog, {
              agentName: deleteDialog.label,
              title: `Delete ${findInstance(deleteDialog.address)?.app.display_name ?? "tab"}`,
              details: DELETE_INSTANCE_DETAILS,
              onConfirm() {
                const dialog = deleteDialog!;
                deleteDialog = null;
                void executeDelete(dialog.address);
              },
              onCancel() {
                deleteDialog = null;
              },
            })
          : null,

        membershipDialog !== null
          ? m(ProjectMembershipDialog, {
              instanceLabel: membershipDialog.label,
              projects: getAvailableProjects(),
              showingProjectIds: membershipDialog.projectIds,
              onConfirm(selectedProjectIds: string[]) {
                const dialog = membershipDialog!;
                membershipDialog = null;
                void applyMembershipSelection(dialog, selectedProjectIds);
              },
              onCancel() {
                membershipDialog = null;
              },
            })
          : null,
      ],
    );
  },
};
