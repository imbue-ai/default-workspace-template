/**
 * Asks whether to keep fast mode after a new chat's grace period.
 *
 * Every way out other than "Keep fast mode on" turns fast mode off -- the
 * buttons, the backdrop, and Escape -- because the cheaper outcome is the one
 * nobody can be surprised by. The copy says so, so dismissing is an informed
 * choice rather than an accident.
 */

import m from "mithril";
import { resolveFastModePrompt } from "../models/WorkspaceFastMode";

export const FastModeModal: m.Component = {
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
        m("div.fast-mode-modal", { role: "dialog", "aria-modal": "true", "aria-label": "Keep fast mode on?" }, [
          m("h3.fast-mode-modal-title", "Keep fast mode on?"),
          m(
            "p.fast-mode-modal-message",
            `This workspace started you on fast mode, which makes replies noticeably quicker while you
             get going. It costs more per token than standard speed, and that usage is billed as
             credits rather than against your plan.`,
          ),
          m(
            "p.fast-mode-modal-message",
            `Your choice applies to new chats in this workspace, and you can change it any time with
             the lightning bolt in the message box.`,
          ),
          m("div.fast-mode-modal-actions", [
            m(
              "button.fast-mode-modal-btn.fast-mode-modal-btn-standard",
              { onclick: () => resolveFastModePrompt(false) },
              "Switch to standard speed",
            ),
            m(
              "button.fast-mode-modal-btn.fast-mode-modal-btn-fast",
              { onclick: () => resolveFastModePrompt(true) },
              "Keep fast mode on",
            ),
          ]),
        ]),
      ],
    );
  },
};

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    resolveFastModePrompt(false);
  }
}
