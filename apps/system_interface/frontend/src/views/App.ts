import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";

export function App(): m.Component {
  return {
    view() {
      // Height comes from the .app-layout rules in theme.css (dvh-aware, with
      // a keyboard clamp from mobile-viewport.ts).
      return m("div", { class: "app-layout flex" }, [
        m("div", { class: "minds-titlebar-spacer" }),
        m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
        // Claude auth is mind-global, so the login modal is a single
        // app-level instance driven by global auth state -- not one per
        // ChatPanel. It opens when any agent surfaces an auth-error.
        isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
      ]);
    },
  };
}
