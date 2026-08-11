import m from "mithril";
import { DockviewWorkspace, getActiveProject, openAppTab, openTabOfType, switchToProject } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { FastModeModal } from "./FastModeModal";
import { ProjectPicker } from "./ProjectPicker";
import { Sidebar } from "./Sidebar";
import type { QuickAddTabType } from "./Sidebar";
import type { AppEntry } from "../models/AgentManager";
import { checkAuthStatusOnLoad, isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";
import { fetchWorkspaceFastMode, getFastModePromptAgentId } from "../models/WorkspaceFastMode";

export function App(): m.Component {
  return {
    oninit() {
      // One-shot page-load auth check: a freshly created mind has no
      // credentials at all (the create flow injects none), so the sign-in
      // modal is the designed first-boot step rather than an error path.
      checkAuthStatusOnLoad();
      // The workspace's fast-mode decision gates the grace-period prompt below.
      // Loaded once here so every ChatPanel can test it without its own request.
      fetchWorkspaceFastMode();
    },
    view() {
      return m(
        "div",
        { class: "app-layout flex flex-col", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" },
        [
          m("div", { class: "minds-titlebar-spacer" }),
          // Which project you are in frames everything below it -- the rail's
          // header, the tabs, what a new tab joins -- so the switcher spans the
          // window rather than sitting inside the rail it also labels.
          m(
            "div",
            { class: "project-bar" },
            m(ProjectPicker, {
              // The picker has already persisted the choice by the time this
              // runs; loading that project's dockview state is the workspace's
              // half of the switch.
              onSelectProject: (projectId: string) => {
                void switchToProject(projectId);
              },
            }),
          ),
          m("div", { class: "app-main flex flex-1 min-w-80" }, [
            m(Sidebar, {
              // Read every draw rather than cached: the registry loads
              // asynchronously and a rename arrives as a redraw, so the rail's
              // header follows both without a subscription of its own.
              project: getActiveProject(),
              onOpenTabType: (tabType: QuickAddTabType) => {
                openTabOfType(tabType);
              },
              onOpenApp: (app: AppEntry) => {
                openAppTab(app);
              },
            }),
            // ``min-w-0`` so a wide tab strip scrolls inside the workspace
            // instead of pushing this row wider than the window.
            m("div", { class: "min-w-0 flex-1" }, m(DockviewWorkspace)),
          ]),
          // Claude auth is mind-global, so the login modal is a single
          // app-level instance driven by global auth state -- not one per
          // ChatPanel. It opens on the load-time check, when any agent
          // surfaces an auth-error, or from the chat footer's "Agent auth"
          // entry.
          isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
          // The fast-mode decision is workspace-wide for the same reason, so one
          // chat reaching the end of its grace period raises a single shared
          // prompt here (see fast-mode-prompt.ts for when that happens).
          getFastModePromptAgentId() !== null ? m(FastModeModal) : null,
        ],
      );
    },
  };
}
