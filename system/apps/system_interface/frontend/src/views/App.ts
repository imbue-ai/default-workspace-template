import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { AgentAuthInstructionsModal } from "./AgentAuthInstructionsModal";
import { FastModeModal } from "./FastModeModal";
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
      return m("div", { class: "app-layout flex", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" }, [
        m("div", { class: "minds-titlebar-spacer" }),
        m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
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
      ]);
    },
  };
}
