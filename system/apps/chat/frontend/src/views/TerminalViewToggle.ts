/**
 * The switch that turns a chat over to its agent's terminal.
 *
 * The chat and the terminal are two renderings of one conversation, so this reads as a view
 * setting rather than a place to navigate to -- which is why it is a switch and not a button,
 * and why it sits in the composer's under-bar next to the model. In the phone layout the model
 * menu carries it as a row instead, and it shows here only while the terminal face is up.
 *
 * It is the shared switch, its label before the drawing; style.css sets the row around it.
 */
import m from "mithril";

import { Switch } from "@imbue/workspace-ui/src/components/formControls";

export interface TerminalViewToggleAttrs {
  on: boolean;
  onToggle: () => void;
}

export const TerminalViewToggle: m.Component<TerminalViewToggleAttrs> = {
  view(vnode) {
    const { on, onToggle } = vnode.attrs;
    return m(Switch, { on, onToggle, label: "Source view", extra: "terminal-view-toggle" });
  },
};
