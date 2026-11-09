/**
 * One keyboard shortcut drawn as a single keycap, written the way a menu writes it ("⇧⌘N" on an Apple platform,
 * "Ctrl+Shift+N" elsewhere), with its name in words for screen readers and on hover.
 */

import m from "mithril";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { chordDisplayLabel, chordSpokenLabel } from "@imbue/workspace-ui/src/keybindings";

export interface ShortcutKeycapAttrs {
  readonly chord: string;
  readonly isApple: boolean;
}

const KEYCAP_CLASS =
  "shortcut-keycap inline-flex h-6 min-w-6 items-center justify-center rounded-md border border-default " +
  "bg-surface px-1.5 font-sans text-(length:--font-size-row) leading-none tracking-wide text-primary " +
  "shadow-[inset_0_-1px_0_var(--c-border)]";

export function ShortcutKeycap(): m.Component<ShortcutKeycapAttrs> {
  return {
    view(vnode) {
      const { chord, isApple } = vnode.attrs;
      const spoken = chordSpokenLabel(chord, isApple);
      return m(
        "kbd",
        { class: KEYCAP_CLASS, "aria-label": spoken, "data-chord": chord, ...hoverTooltipAttrs(spoken) },
        chordDisplayLabel(chord, isApple),
      );
    },
  };
}
