/**
 * The one-time notices' shared chrome: a small bordered card with an accent icon, one sentence
 * and a dismiss button, which a notice places and words itself.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";

export function renderDismissibleNotice(opts: {
  // The notice's own class and any positioning utilities.
  extraClass: string;
  // The accent icon's markup (an `icon(...)` string).
  iconHtml: string;
  text: string;
  // The dismiss button's own class, by which a test finds it.
  dismissExtra: string;
  onDismiss: () => void;
}): m.Vnode {
  return m(
    "div",
    {
      class:
        `${opts.extraClass} flex max-w-[360px] items-start gap-2 rounded-lg border border-default bg-surface ` +
        "px-3 py-2 text-(length:--font-size-helper) text-secondary shadow-md",
      role: "status",
    },
    [
      m("span", { class: "mt-0.5 shrink-0 text-accent" }, m.trust(opts.iconHtml)),
      m("span", { class: "min-w-0" }, opts.text),
      m(
        Button,
        {
          variant: "ghost",
          icon: true,
          xs: true,
          extra: `${opts.dismissExtra} shrink-0`,
          "aria-label": "Dismiss",
          onclick: opts.onDismiss,
        },
        m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
      ),
    ],
  );
}
