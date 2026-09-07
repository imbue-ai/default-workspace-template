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
  switchToView,
  requestAppLifecycle,
  requestInstanceLifecycle,
} from "./DockviewWorkspace";
import { Sidebar } from "./Sidebar";
import { UpdateStalenessBanner } from "./UpdateStalenessBanner";
import type { SidebarTabRow } from "./Sidebar";
import type { AppAction, AppRecord, ProjectShortcut, ShortcutMode } from "../models/Inventory";

export function App(): m.Component {
  return {
    view() {
      return m(
        "div",
        // h-screen is the full viewport: inside the minds desktop shell this app renders in a
        // sandboxed iframe the shell already places below its title bar.
        { class: "app-layout flex h-screen flex-col" },
        [
          m(UpdateStalenessBanner),
          // min-h-0: a flex item's automatic minimum size is its content's, so without this the
          // row can grow with the viewport but never shrink back.
          // pt/pl-1: the canvas runs edge to edge, with the padding as the outermost pane gap (the
          // left so the rail has a few pixels of canvas to hover into).
          m("div", { class: "app-main flex min-h-0 flex-1 min-w-80 bg-(--si-canvas) pt-1 pl-1" }, [
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
                // A new project opens on its New Tab page; what goes in it is the user's call.
                void switchToView(projectId);
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
              onInstanceLifecycle: (row: SidebarTabRow, action: "stop" | "start") => {
                requestInstanceLifecycle(row.appName, row.instanceKey, action);
              },
              onDeleteRow: (row: SidebarTabRow) => {
                deleteAddress(row.address);
              },
            }),
            // ``min-w-0`` so a wide tab strip scrolls inside the workspace instead of pushing
            // this row wider than the window.
            m("div", { class: "min-w-0 flex-1" }, m(DockviewWorkspace)),
          ]),
        ],
      );
    },
  };
}
