import m from "mithril";
import {
  DockviewWorkspace,
  addMemberRowToProjects,
  destroyMemberRow,
  focusLastOfShortcut,
  getActiveViewId,
  getAvailableProjects,
  getAwaitingShortcutIds,
  getSidebarRows,
  openAppShortcut,
  openMemberRow,
  openNewOfShortcut,
  openTabOfType,
  refreshMemberRow,
  refreshProjects,
  removeMemberRow,
  renameMemberRowWithAlert,
  setAppPinnedInView,
  setShortcutModeInView,
  setShortcutPinnedInView,
  shareMemberRow,
  startProjectChat,
  stopChatRow,
  switchToView,
  toggleAppLifecycle,
} from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { AgentAuthInstructionsModal } from "./AgentAuthInstructionsModal";
import { FastModeModal } from "./FastModeModal";
import { Sidebar } from "./Sidebar";
import type { QuickAddTabType, SidebarTabRow } from "./Sidebar";
import type { AppEntry } from "../models/AgentManager";
import { checkAuthStatusOnLoad, isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";
import { getAuthInstructionsAgentId } from "../models/AgentAuth";
import { getFastModePromptAgentId } from "../models/FastModePrompt";

export function App(): m.Component {
  return {
    oninit() {
      // One-shot page-load auth check: a freshly created mind has no
      // credentials at all (the create flow injects none), so the sign-in
      // modal is the designed first-boot step rather than an error path.
      checkAuthStatusOnLoad();
    },
    view() {
      return m(
        "div",
        // h-screen is the full viewport: inside the minds desktop shell this
        // app renders in a sandboxed iframe the shell already places below its
        // title bar (WorkspaceFrame's .workspace-surface starts at top: 38px),
        // so there is no title-bar height to subtract here. (A vestigial
        // --minds-titlebar-height calc + spacer div once did that subtraction,
        // but nothing ever set the variable inside this iframe.)
        { class: "app-layout flex h-screen flex-col" },
        [
          // The whole content area is one grey surface with the rail sitting on
          // it, directly left of the dock. Which view you are in is said by the
          // rail's own header now, so there is no bar above this row.
          //
          // min-h-0: a flex item's automatic minimum size is its content's, so
          // without this the row can grow with the viewport but never shrink
          // back. The dock then keeps the height it was laid out at, and
          // everything it positions in pixels -- the panes, and the live
          // surfaces mirroring them -- hangs below the viewport with the
          // composer's model bar clipped off the bottom. The minds shell
          // shrinking this window for its recovery band is how that happens
          // without the user touching the window.
          // pt/pl-1: the canvas runs edge to edge, with the padding as the
          // outermost pane gap -- the top against the title bar, the left so
          // the rail has a few pixels of canvas to hover into. Right and
          // bottom stay flush against the viewport. The --si-* theme vars the
          // canvas colour comes from live on .app-main in style.css.
          m("div", { class: "app-main flex min-h-0 flex-1 min-w-80 bg-(--si-canvas) pt-1 pl-1" }, [
            // Every attr is read straight off the workspace on each draw rather
            // than cached: the registry loads asynchronously, and a rename, a
            // new tab or another client's change all arrive as a redraw, so the
            // rail follows all of them without a subscription of its own.
            m(Sidebar, {
              projects: getAvailableProjects(),
              activeViewId: getActiveViewId(),
              rows: getSidebarRows(),
              onSelectView: (viewId: string) => {
                // The workspace saves the outgoing layout and swaps the dock;
                // that is the whole of a view switch.
                void switchToView(viewId);
              },
              onProjectsChanged: () => {
                refreshProjects();
              },
              onProjectCreated: (projectId: string) => {
                // Mount the new project, THEN start the one chat it is made
                // with: the mount tears the dock down and rebuilds it, so a
                // chat tab opened before it lands would be swept away with the
                // outgoing layout.
                void switchToView(projectId).then(() => startProjectChat(projectId));
              },
              onOpenTabType: (tabType: QuickAddTabType) => {
                openTabOfType(tabType);
              },
              onOpenApp: (app: AppEntry) => {
                // Mode-aware: focus (the default) goes to the app's existing
                // pane, new opens another pane on the same service.
                openAppShortcut(app);
              },
              onSetAppPinned: (app: AppEntry, isPinned: boolean) => {
                setAppPinnedInView(app, isPinned);
              },
              onSetShortcutPinned: (shortcut: QuickAddTabType, isPinned: boolean) => {
                setShortcutPinnedInView(shortcut, isPinned);
              },
              onSetShortcutMode: (shortcutId: string, mode) => {
                setShortcutModeInView(shortcutId, mode);
              },
              onNewOfKind: (shortcutId: string) => {
                openNewOfShortcut(shortcutId);
              },
              onFocusLastOfKind: (shortcutId: string) => {
                focusLastOfShortcut(shortcutId);
              },
              awaitingShortcutIds: getAwaitingShortcutIds(),
              onOpenRow: (row: SidebarTabRow) => {
                openMemberRow(row);
              },
              onRefreshRow: (row: SidebarTabRow) => {
                refreshMemberRow(row);
              },
              onRenameRow: (row: SidebarTabRow, title: string) => {
                renameMemberRowWithAlert(row, title);
              },
              onRemoveFromView: (row: SidebarTabRow) => {
                removeMemberRow(row);
              },
              onShareApp: (row: SidebarTabRow) => {
                shareMemberRow(row);
              },
              onAddRowToProjects: (row: SidebarTabRow) => {
                addMemberRowToProjects(row);
              },
              onStopRow: (row: SidebarTabRow) => {
                stopChatRow(row);
              },
              onServiceLifecycle: (serviceName: string) => {
                toggleAppLifecycle(serviceName);
              },
              onDeleteFromMachine: (row: SidebarTabRow) => {
                destroyMemberRow(row);
              },
            }),
            // ``min-w-0`` so a wide tab strip scrolls inside the workspace
            // instead of pushing this row wider than the window. The rail is
            // absolutely positioned inside its own 37px slot, so expanding it
            // overlays this dock rather than squeezing it.
            m("div", { class: "min-w-0 flex-1" }, m(DockviewWorkspace)),
          ]),
          // Claude auth is mind-global, so the login modal is a single
          // app-level instance driven by global auth state -- not one per
          // ChatPanel. It opens on the load-time check, when any agent
          // surfaces an auth-error, or from the chat footer's "Agent auth"
          // entry.
          isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
          // The terminal-auth counterpart: harnesses whose sign-in runs in their
          // own TUI raise this shared instructions notice instead (see AgentAuth.ts).
          getAuthInstructionsAgentId() !== null ? m(AgentAuthInstructionsModal) : null,
          // One chat reaching the end of its fast-mode grace period raises a single
          // shared prompt here (see fast-mode-prompt.ts for when that happens).
          getFastModePromptAgentId() !== null ? m(FastModeModal) : null,
        ],
      );
    },
  };
}
