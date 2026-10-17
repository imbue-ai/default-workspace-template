/** The small ghost button with only a glyph on it that the toolbars and rows carry, named by its label. */

import m from "mithril";

import { Button } from "@imbue/workspace-ui/src/components/Button";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon, type IconName } from "@imbue/workspace-ui/src/components/icons";

/** `tooltip: false` for a menu's trigger, where the label would sit over the menu it opens. */
export function iconButton(
  label: string,
  name: IconName,
  attrs: m.Attributes,
  options: { tooltip?: boolean } = {},
): m.Vnode {
  return m(
    Button,
    {
      variant: "ghost",
      icon: true,
      sm: true,
      "aria-label": label,
      ...(options.tooltip === false ? {} : hoverTooltipAttrs(label)),
      ...attrs,
    },
    m.trust(icon(name, { size: 16 })),
  );
}
