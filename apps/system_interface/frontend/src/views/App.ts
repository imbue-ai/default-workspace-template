import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { Header } from "./Header";
import { SettingsModal } from "./SettingsModal";
import { isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";
import { isSettingsOpen, closeSettings } from "../models/Settings";

export function App(): m.Component {
  return {
    view() {
      return m(
        "div",
        { class: "app-layout flex flex-col", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" },
        [
          m("div", { class: "minds-titlebar-spacer" }),
          m(Header),
          m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
          // Claude auth is mind-global, so the login modal is a single
          // app-level instance driven by global auth state -- not one per
          // ChatPanel. It opens when any agent surfaces an auth-error, and now
          // also when the user picks Sign in from Settings.
          isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
          // Settings is likewise mind-global: one app-level overlay toggled by
          // the header gear button.
          isSettingsOpen() ? m(SettingsModal, { onClose: closeSettings }) : null,
        ],
      );
    },
  };
}
