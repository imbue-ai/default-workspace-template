import m from "mithril";
import {
  DockviewWorkspace,
  addAddressToProjects,
  deleteAddress,
  focusLastOfShortcut,
  getActiveViewId,
  getAvailableProjects,
  getAwaitingActionKeys,
  getSidebarRows,
  openAddress,
  refreshAddress,
  refreshProjects,
  removeAddressFromView,
  removeShortcutFromView,
  renameAddress,
  runAppAction,
  runShortcut,
  runShortcutAsNew,
  setShortcutInView,
  shareApp,
  startChatOnAccount,
  startProjectChat,
  switchToView,
  requestAppLifecycle,
} from "./DockviewWorkspace";
import { ProviderChooserModal } from "./ProviderChooserModal";
import { Sidebar } from "./Sidebar";
import { UpdateStalenessBanner } from "./UpdateStalenessBanner";
import type { SidebarTabRow } from "./Sidebar";
import type { AppAction, AppRecord, ProjectShortcut, ShortcutMode } from "../models/Inventory";
import {
  closeProviderChooser,
  getAccounts,
  isProviderChooserOpen,
  loadAccountsWithRetry,
  openProviderChooser,
} from "../models/Providers";

/** Marks that the workspace has already greeted this user. Local storage rather than a server
 *  flag on purpose: it is a fact about a person having seen a screen. */
const GREETED_KEY = "minds.provider-chooser.greeted";

/** Open the provider chooser the FIRST time a workspace is opened with nothing signed in. It
 *  fires once, ever. Signing in from the greeting STARTS a chat rather than closing onto an
 *  empty new tab. */
function greetFirstRun(): void {
  if (getAccounts().length > 0) return;
  try {
    if (window.localStorage.getItem(GREETED_KEY) !== null) return;
    window.localStorage.setItem(GREETED_KEY, "1");
  } catch {
    return;
  }
  openProviderChooser({ onSignedIn: (accountId) => void startChatOnAccount(accountId) });
  m.redraw();
}

export function App(): m.Component {
  return {
    oninit() {
      // The new-tab picker and the rail's chat shortcut both read the account list, and both
      // can be the first thing a user clicks, so load it once at boot, retried until it succeeds.
      void loadAccountsWithRetry().then(greetFirstRun);
    },
    view() {
      return m(
        "div",
        { class: "app-layout flex flex-col", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" },
        [
          m("div", { class: "minds-titlebar-spacer" }),
          m(UpdateStalenessBanner),
          // min-h-0: a flex item's automatic minimum size is its content's, so without this the
          // row can grow with the viewport but never shrink back.
          m("div", { class: "app-main flex min-h-0 flex-1 min-w-80" }, [
            // Every attr is read straight off the workspace on each draw rather than cached: the
            // inventory and the projects arrive over the socket as redraws.
            m(Sidebar, {
              projects: getAvailableProjects(),
              activeViewId: getActiveViewId(),
              rows: getSidebarRows(),
              onSelectView: (viewId: string) => {
                void switchToView(viewId);
              },
              onProjectsChanged: () => {
                refreshProjects();
              },
              onProjectCreated: (projectId: string) => {
                // Mount the new project, THEN start the one chat it is made with: the mount
                // tears the dock down and rebuilds it.
                void switchToView(projectId).then(() => startProjectChat(projectId));
              },
              onRunShortcut: (shortcut: ProjectShortcut) => {
                runShortcut(shortcut);
              },
              onRunShortcutAsNew: (shortcut: ProjectShortcut) => {
                runShortcutAsNew(shortcut);
              },
              onFocusLastOfShortcut: (shortcut: ProjectShortcut) => {
                focusLastOfShortcut(shortcut);
              },
              onSetShortcutMode: (shortcut: ProjectShortcut, mode: ShortcutMode) => {
                setShortcutInView(shortcut.app, shortcut.action, mode);
              },
              onRemoveShortcut: (shortcut: ProjectShortcut) => {
                removeShortcutFromView(shortcut.app, shortcut.action);
              },
              onPinShortcut: (app: AppRecord, action: AppAction) => {
                setShortcutInView(app.name, action.id, "focus");
              },
              onRunAppAction: (app: AppRecord, action: AppAction) => {
                runAppAction(app, action.id);
              },
              awaitingActionKeys: getAwaitingActionKeys(),
              onOpenRow: (row: SidebarTabRow) => {
                openAddress(row.address);
              },
              onRefreshRow: (row: SidebarTabRow) => {
                refreshAddress(row.address);
              },
              onRenameRow: (row: SidebarTabRow, title: string) => {
                renameAddress(row.address, title);
              },
              onShareApp: (appName: string) => {
                shareApp(appName);
              },
              onAddRowToProjects: (row: SidebarTabRow) => {
                addAddressToProjects(row.address);
              },
              onRemoveFromView: (row: SidebarTabRow) => {
                removeAddressFromView(row.address);
              },
              onAppLifecycle: (appName: string, action: "stop" | "start") => {
                requestAppLifecycle(appName, action);
              },
              onDeleteRow: (row: SidebarTabRow) => {
                deleteAddress(row.address);
              },
            }),
            // ``min-w-0`` so a wide tab strip scrolls inside the workspace instead of pushing
            // this row wider than the window.
            m("div", { class: "min-w-0 flex-1" }, m(DockviewWorkspace)),
          ]),
          // The provider chooser: one app-level instance, because accounts are mind-global.
          isProviderChooserOpen() ? m(ProviderChooserModal, { onDismiss: closeProviderChooser }) : null,
        ],
      );
    },
  };
}
