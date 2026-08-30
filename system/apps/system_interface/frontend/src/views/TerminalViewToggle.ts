/**
 * The switch that turns a chat over to its agent's terminal.
 *
 * The chat and the terminal are two renderings of one conversation, so this reads as a view
 * setting rather than a place to navigate to -- which is why it is a switch and not a button,
 * and why it sits in the composer's under-bar next to the model.
 *
 * It is the SAME switch as the combo card's fast-mode row, classes and all. An earlier version
 * scaled it down here with its own CSS, which set `transform: translateX(...)` on a knob whose
 * offset already comes from a Tailwind `translate-x-[22px]` utility -- the utility won, and a
 * 22px throw in a 32px track put the knob outside it. One switch, one size, no overrides.
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
        "aria-label": "Terminal View",
        onclick: onToggle,
      },
      [
        m("span", { class: "terminal-view-toggle-label" }, "Terminal View"),
        m(
          "span",
          { class: `${css.SWITCH} ${on ? css.SWITCH_ON : css.SWITCH_OFF}` },
          m("span", { class: `${css.SWITCH_KNOB} ${on ? css.SWITCH_KNOB_ON : css.SWITCH_KNOB_OFF}` }),
        ),
      ],
    );
  },
};
