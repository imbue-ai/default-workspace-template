/**
 * The switch that turns a chat over to its agent's terminal.
 *
 * The chat and the terminal are two renderings of one conversation, so this reads as a view
 * setting rather than a place to navigate to -- which is why it is a switch and not a button,
 * and why it sits in the composer's under-bar next to the model.
 *
 * It is the model menu's switch at its `sm` size. Sitting in the under-bar beside a line of
 * helper text, it has to read as that text's equal rather than as the loudest thing down
 * there -- so the label takes the faint role and the switch takes `sm`. The size comes from
 * `SWITCH_SIZES` by name, never from scaling the switch here: a track and its knob's throw
 * only agree when they are handed out together.
 */
import m from "mithril";

import * as css from "./modelProviderMenuStyles";

export interface TerminalViewToggleAttrs {
  on: boolean;
  /** Receives the click event so the caller can locate its own panel in the DOM. */
  onToggle: (event: Event) => void;
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
        "aria-label": "Source view",
        onclick: onToggle,
      },
      [
        m("span", { class: "terminal-view-toggle-label" }, "Source view"),
        m(
          "span",
          { class: `${css.switchClass("sm")} ${on ? css.SWITCH_ON : css.SWITCH_OFF}` },
          m("span", { class: css.switchKnobClass("sm", on) }),
        ),
      ],
    );
  },
};
