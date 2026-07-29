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
 * on.
 */

import m from "mithril";
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
