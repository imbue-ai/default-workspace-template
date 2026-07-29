/**
 * Asks whether to keep fast mode after a new chat's grace period.
 *
 * It is shown as a popover above the composer's lightning-bolt toggle -- the
 * control that answers the same question every time after this one -- and falls
 * back to the middle of the screen when that toggle cannot be found or has no
 * room above it (see fast-mode-anchor.ts).
 *
 * Every way out other than "Keep fast mode on" turns fast mode off -- the
 * buttons, the backdrop, and Escape -- because the cheaper outcome is the one
 * nobody can be surprised by. It is also the button the popover opens focused
 * on, and the copy says what dismissing does, so leaving is an informed choice
 * rather than an accident.
 */

import m from "mithril";
import { getSelectedOption } from "../models/ModelSettings";
import { getFastModePromptAgentId, resolveFastModePrompt } from "../models/WorkspaceFastMode";
import { computeAnchoredPosition, type AnchoredPosition } from "./fast-mode-anchor";
import { icon } from "./icons";

const FAST_MODE_DOC_URL = "https://code.claude.com/docs/en/fast-mode";

export function FastModeModal(): m.Component {
  let anchor: AnchoredPosition | null = null;

  // Measured against the toggle of the chat that raised the prompt: other chats
  // have their own composer, and a hidden one would put the popover nowhere.
  function measureAnchor(): void {
    const agentId = getFastModePromptAgentId();
    const toggle = Array.from(document.querySelectorAll<HTMLElement>(".fast-toggle")).find(
      (element) => element.dataset.agentId === agentId,
    );
    anchor =
      toggle === undefined
        ? null
        : computeAnchoredPosition(toggle.getBoundingClientRect(), {
            width: window.innerWidth,
            height: window.innerHeight,
          });
  }

  function remeasureAnchor(): void {
    measureAnchor();
    m.redraw();
  }

  return {
    // Measured before the first render, rather than in `oncreate`, so the
    // popover is never painted centered and then moved.
    oninit() {
      measureAnchor();
    },

    oncreate() {
      document.addEventListener("keydown", handleKeydown);
      window.addEventListener("resize", remeasureAnchor);
    },

    onremove() {
      document.removeEventListener("keydown", handleKeydown);
      window.removeEventListener("resize", remeasureAnchor);
    },

    view() {
      const anchorStyle =
        anchor === null
          ? undefined
          : {
              left: `${anchor.left}px`,
              bottom: `${anchor.bottom}px`,
              width: `${anchor.width}px`,
              "--fast-mode-arrow-left": `${anchor.arrowLeft}px`,
            };
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
              class: anchor === null ? undefined : "fast-mode-modal--anchored",
              style: anchorStyle,
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
                `${describeModel()} is provided by Anthropic, who offers a "fast mode" in which the model
                 works much faster but for a significantly higher price. This workspace started with it
                 enabled to get you going faster. `,
                m(
                  "a.fast-mode-modal-link",
                  { href: FAST_MODE_DOC_URL, target: "_blank", rel: "noopener noreferrer" },
                  [m("span", "Anthropic's docs"), m.trust(icon("external-link", { size: 13 }))],
                ),
              ]),
              m(
                "p.fast-mode-modal-message",
                `You can choose whether to keep it enabled or disable it by default for this and future
                 chats. You can always toggle fast mode using the lightning button.`,
              ),
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

/** The chat's model by name ("The Opus 4.8 model"), or a generic stand-in while
 *  its settings have not loaded. Named rather than hardcoded because more than
 *  one model supports fast mode and the catalog changes. */
function describeModel(): string {
  const agentId = getFastModePromptAgentId();
  const label = agentId === null ? null : getSelectedOption(agentId)?.label;
  return label === null || label === undefined ? "The model this chat runs on" : `The ${label} model`;
}

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    resolveFastModePrompt(false);
  }
}
