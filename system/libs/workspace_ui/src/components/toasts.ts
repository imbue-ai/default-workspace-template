/**
 * The toasts of ``models/toasts.ts``, stacked at the foot of the page, each going on its own. The stack is a live
 * region that stays mounted while empty: a screen reader announces a note added to a region it already knows, not
 * one that arrives with it.
 *
 * A toast takes no press unless the page offers ``onDismiss``: the shell's never stand between the user and what is
 * under them, while a page whose notes run longer (Files lists what failed) gives each a close button.
 */

import m from "mithril";

import type { Toast } from "../models/toasts";
import { icon } from "./icons";

export interface ToastsAttrs {
  readonly toasts: readonly Toast[];
  /** Where the stack sits: ``fixed`` over the viewport, or ``absolute`` inside a layout, and how far up. */
  readonly placementClass: string;
  /** Give each toast a close button that calls this with its id. */
  readonly onDismiss?: (id: number) => void;
}

export const Toasts: m.Component<ToastsAttrs> = {
  view(vnode) {
    const { toasts, placementClass, onDismiss } = vnode.attrs;
    return m(
      "div",
      {
        role: "status",
        "aria-live": "polite",
        class:
          `toasts pointer-events-none inset-x-0 z-(--z-overlay) flex flex-col items-center gap-2 px-4 ` +
          placementClass,
      },
      toasts.map((toast) =>
        m(
          "div",
          {
            key: toast.id,
            "data-toast": "",
            // A pill on one line, and still a card once a longer note wraps.
            class:
              "toast flex max-w-[min(480px,100%)] animate-[toast-rise_var(--dur-slow)_ease-out] items-start gap-3 " +
              "rounded-[20px] bg-inverse px-4 py-2.5 text-(length:--font-size-body) whitespace-pre-line " +
              `text-on-accent shadow-overlay ${onDismiss === undefined ? "" : "pointer-events-auto"}`,
          },
          [
            m("span", { class: "min-w-0 flex-1" }, toast.message),
            onDismiss === undefined
              ? null
              : m(
                  "button",
                  {
                    type: "button",
                    "aria-label": "Dismiss",
                    "data-toast-dismiss": "",
                    class: "flex h-5 shrink-0 cursor-pointer items-center text-white/70 hover:text-white",
                    onclick: () => onDismiss(toast.id),
                  },
                  m.trust(icon("close", { size: 14 })),
                ),
          ],
        ),
      ),
    );
  },
};
