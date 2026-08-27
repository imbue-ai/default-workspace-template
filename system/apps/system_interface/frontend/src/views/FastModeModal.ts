/**
 * Asks whether to keep fast mode after the first chat's grace period.
 *
 * Asked once per agent: every way out records the answer (see
 * models/FastModePrompt.ts). Every way out other than "Keep fast mode on" also
 * turns fast mode off -- the buttons, the backdrop, and Escape -- because the
 * cheaper outcome is the one nobody can be surprised by. It is also the button
 * the modal opens focused on. The answer applies only to this agent; no other
 * chat launches fast in the first place.
 */

import m from "mithril";
import { MODAL_MESSAGE_CLASS, MODAL_TITLE_CLASS, Modal } from "./Modal";
import { getAgentById } from "../models/AgentManager";
import { getFastModePromptAgentId, resolveFastModePrompt } from "../models/FastModePrompt";
import { icon } from "./icons";
import { Button } from "./Button";

const FAST_MODE_DOC_URL = "https://code.claude.com/docs/en/fast-mode";

/** The name of the agent that raised the prompt, for the modal copy. */
function promptingAgentName(): string | null {
  const agentId = getFastModePromptAgentId();
  if (agentId === null) {
    return null;
  }
  return getAgentById(agentId)?.name ?? null;
}

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
        Modal,
        {
          onDismiss: () => resolveFastModePrompt(false),
          width: 460,
          card: {
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Keep fast mode on?",
          },
          header: [
            m(
              "span",
              {
                class:
                  "fast-mode-modal-icon inline-flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-lg bg-accent-light text-accent",
              },
              m.trust(icon("zap", { size: 16 })),
            ),
            m("h3", { class: MODAL_TITLE_CLASS }, "Keep fast mode on?"),
          ],
          actions: [
            m(Button, { onclick: () => resolveFastModePrompt(true) }, "Keep fast mode on"),
            m(
              Button,
              {
                variant: "primary",
                onclick: () => resolveFastModePrompt(false),
                // The default action, so Enter takes it without a reach for the mouse.
                oncreate: (vnode) => {
                  (vnode.dom as HTMLButtonElement).focus();
                },
              },
              "Switch to standard speed",
            ),
          ],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, [
            promptingAgentName() !== null ? [m("strong", promptingAgentName()), " has Fast Mode on. "] : null,
            "Fast Mode is 2.5x faster and 2x more expensive (",
            m(
              "a",
              {
                class: "fast-mode-modal-link inline-flex items-center gap-1 whitespace-nowrap text-accent underline",
                href: FAST_MODE_DOC_URL,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              [m("span", "learn more"), m.trust(icon("external-link", { size: 13 }))],
            ),
            ")",
          ]),
          m("p", { class: MODAL_MESSAGE_CLASS }, [
            "You can toggle Fast Mode at any time with the ",
            // A copy of the composer's toggle, so "the button" has something to
            // point at. Decorative: hidden from assistive tech, which gets the
            // sentence on its own.
            m("span.toggle.toggle--on.toggle--inline", { "aria-hidden": "true" }, m.trust(icon("zap", { size: 16 }))),
            " button",
          ]),
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
