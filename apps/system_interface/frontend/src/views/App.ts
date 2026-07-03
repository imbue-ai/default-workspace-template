import m from "mithril";
import { DockviewWorkspace } from "./DockviewWorkspace";
import { ClaudeLoginModal } from "./ClaudeLoginModal";
import { isLoginModalOpen, closeLoginModal } from "../models/ClaudeAuth";
import { InspirationPublishModal } from "./InspirationPublishModal";
import { GitHubLoginModal } from "./GitHubLoginModal";
import {
  getInspirationProposal,
  getInspirationProposalSlug,
  closeInspirationModal,
} from "../models/InspirationPublish";
import { isGitHubLoginModalOpen, closeGitHubLoginModal } from "../models/GitHubAuth";

export function App(): m.Component {
  return {
    view() {
      return m("div", { class: "app-layout flex", style: "height: calc(100vh - var(--minds-titlebar-height, 0px))" }, [
        m("div", { class: "minds-titlebar-spacer" }),
        m("div", { class: "app-main flex flex-1 min-w-80" }, [m(DockviewWorkspace)]),
        // Claude auth is mind-global, so the login modal is a single
        // app-level instance driven by global auth state -- not one per
        // ChatPanel. It opens when any agent surfaces an auth-error.
        isLoginModalOpen() ? m(ClaudeLoginModal, { onDismiss: closeLoginModal }) : null,
        // GitHub-login modal: single app-level singleton, opened by the
        // `github_auth_required` broadcast (mirrors the Claude modal).
        isGitHubLoginModalOpen() ? m(GitHubLoginModal, { onDismiss: closeGitHubLoginModal }) : null,
        // Inspiration publish modal: keyed by proposal slug so a superseding
        // proposal (new slug) forces a fresh instance that re-prefills its form.
        // The keyed vnode MUST be wrapped in its own single-child fragment
        // (the nested array): mithril requires a children list to be either
        // all-keyed or all-unkeyed, and this app-level children array is
        // unkeyed. A bare keyed child here makes every redraw throw
        // "In fragments, vnodes must either all have keys or none have keys"
        // the moment a proposal arrives, so the modal never renders.
        getInspirationProposal()
          ? [
              m(InspirationPublishModal, {
                // The modal only renders when a proposal is present, so the slug
                // is always a string here; coerce the nullable accessor to
                // `undefined` to satisfy mithril's `key: string | number`.
                key: getInspirationProposalSlug() ?? undefined,
                proposal: getInspirationProposal()!,
                onDismiss: () => closeInspirationModal(getInspirationProposalSlug()),
              }),
            ]
          : null,
      ]);
    },
  };
}
