/**
 * Asks whether to keep fast mode after a new chat's grace period.
 *
 * Every way out other than "Keep fast mode on" turns fast mode off -- the
 * buttons, the backdrop, and Escape -- because the cheaper outcome is the one
 * nobody can be surprised by. It is also the button the modal opens focused
 * on.
 */

import m from "mithril";
import { resolveFastModePrompt } from "../models/WorkspaceFastMode";
import { icon } from "./icons";

const FAST_MODE_DOC_URL = "https://code.claude.com/docs/en/fast-mode";

export function FastModeModal(): m.Component {
  return {
    oncreate() {
      document.addEventListener("keydown", handleKeydown);
    },

    onremove() {
      document.removeEventListener("keydown", handleKeydown);
    },

    view() {
      return m(
        "div.fast-mode-modal-overlay",
        {
          onclick: (event: Event) => {
            if (event.target === event.currentTarget) {
              resolveFastModePrompt(false);
            }
          },
        },
        [
          m(
            "div.fast-mode-modal",
            {
              role: "dialog",
              "aria-modal": "true",
              "aria-label": "Keep fast mode on?",
            },
            [
              m("div.fast-mode-modal-header", [
                m("span.fast-mode-modal-icon", m.trust(icon("zap", { size: 16 }))),
                m("h3.fast-mode-modal-title", "Keep fast mode on?"),
              ]),
              m("p.fast-mode-modal-message", [
                "Fast Mode is 2.5x faster and 6x more expensive (",
                m(
                  "a.fast-mode-modal-link",
                  { href: FAST_MODE_DOC_URL, target: "_blank", rel: "noopener noreferrer" },
                  [m("span", "learn more"), m.trust(icon("external-link", { size: 13 }))],
                ),
                ")",
              ]),
              m("p.fast-mode-modal-message", "You can toggle Fast Mode at any time with the button"),
              m("div.fast-mode-modal-actions", [
                m(
                  "button.fast-mode-modal-btn.fast-mode-modal-btn-fast",
                  { onclick: () => resolveFastModePrompt(true) },
                  "Keep fast mode on",
                ),
                m(
                  "button.fast-mode-modal-btn.fast-mode-modal-btn-standard",
                  {
                    onclick: () => resolveFastModePrompt(false),
                    // The default action, so Enter takes it without a reach for the mouse.
                    oncreate: (vnode: m.VnodeDOM) => {
                      (vnode.dom as HTMLButtonElement).focus();
                    },
                  },
                  "Switch to standard speed",
                ),
              ]),
            ],
          ),
        ],
      );
    },
  };
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    resolveFastModePrompt(false);
  }
}
