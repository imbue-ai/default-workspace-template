/**
 * The switch that turns a chat over to its agent's terminal.
 *
 * The chat and the terminal are two renderings of one conversation, so this reads as a view
 * setting rather than a place to navigate to -- which is why it is a switch and not a button,
 * and why it sits in the composer's under-bar next to the model rather than in a tab strip.
 *
 * Ported from the mockup's `TerminalViewToggle` (`ClaudeCodeView.tsx`). The track and knob are
 * the same shape the combo card's fast-mode switch uses, at the under-bar's smaller size.
 */
import m from "mithril";

import * as css from "./modelCardStyles";

export interface TerminalViewToggleAttrs {
  on: boolean;
  onToggle: () => void;
}

export const TerminalViewToggle: m.Component<TerminalViewToggleAttrs> = {
  view(vnode) {
    const { on, onToggle } = vnode.attrs;
    return m(
      "button",
      {
        type: "button",
        role: "switch",
        class: "terminal-view-toggle",
        "aria-checked": on ? "true" : "false",
        "aria-label": "Terminal view",
        onclick: onToggle,
      },
      [
        m("span", { class: "terminal-view-toggle-label" }, "Terminal"),
        m(
          "span",
          { class: `${css.SWITCH} terminal-view-toggle-track ${on ? css.SWITCH_ON : css.SWITCH_OFF}` },
          m("span", {
            class: `${css.SWITCH_KNOB} terminal-view-toggle-knob ${on ? css.SWITCH_KNOB_ON : css.SWITCH_KNOB_OFF}`,
          }),
        ),
      ],
    );
  },
};
