/**
 * One keybinding chord drawn as keycaps (the Mac's modifier symbols on an Apple platform, spelled-out names
 * elsewhere), with its name in words for screen readers and on hover.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { chordKeycapLabels, chordSpokenLabel } from "@imbue/workspace-ui/src/keybindings";

export interface ChordKeycapsAttrs {
  readonly chord: string;
  readonly isApple: boolean;
}

const KEYCAP_CLASS =
  "keycap inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-default bg-page px-1 " +
  "font-sans text-(length:--font-size-row) leading-none text-secondary";

export function ChordKeycaps(): m.Component<ChordKeycapsAttrs> {
  return {
    view(vnode) {
      const { chord, isApple } = vnode.attrs;
      const spoken = chordSpokenLabel(chord, isApple);
      return m(
        "span",
        {
          class: "chord-keycaps inline-flex items-center gap-0.5",
          role: "img",
          "aria-label": spoken,
          "data-chord": chord,
          ...hoverTooltipAttrs(spoken),
        },
        chordKeycapLabels(chord, isApple).map((label) =>
          m("kbd", { class: KEYCAP_CLASS, "aria-hidden": "true" }, label),
        ),
      );
    },
  };
}
