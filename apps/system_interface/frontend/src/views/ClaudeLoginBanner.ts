/**
 * Non-blocking banner shown at the top of the chat panel after the user
 * dismisses the Claude login modal without signing in. Clicking it
 * re-opens the modal.
 */

import m from "mithril";

export interface ClaudeLoginBannerAttrs {
  onClick: () => void;
}

export const ClaudeLoginBanner: m.Component<ClaudeLoginBannerAttrs> = {
  view(vnode: m.Vnode<ClaudeLoginBannerAttrs>) {
    return m(
      "button",
      {
        type: "button",
        class: "claude-login-banner w-full px-4 py-2 text-left bg-yellow-100 border-b border-yellow-300 hover:bg-yellow-200",
        onclick: vnode.attrs.onClick,
        style: "background: #fef3c7; border-bottom: 1px solid #fcd34d; padding: 8px 16px; text-align: left; cursor: pointer; width: 100%; font-size: 0.9em;",
      },
      "Claude isn't signed in. Click to recover.",
    );
  },
};
