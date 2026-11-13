/**
 * The switch that turns a chat over to its agent's terminal.
 *
 * The chat and the terminal are two renderings of one conversation, so this reads as a view
 * setting rather than a place to navigate to -- which is why it is a switch and not a button,
 * and why it sits in the composer's under-bar next to the model. In the phone layout the model
 * menu carries it as a row instead, and it shows here only while the terminal face is up.
 *
 * It is the shared switch's drawing (`switchVisual`), the button around it carrying the switch's role.
 */
import m from "mithril";

import { switchVisual } from "@imbue/workspace-ui/src/components/formControls";

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
        "aria-label": "Source view",
        onclick: () => onToggle(),
      },
      [m("span", { class: "terminal-view-toggle-label" }, "Source view"), switchVisual(on)],
    );
  },
};
