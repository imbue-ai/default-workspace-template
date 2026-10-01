/**
 * The shell's toasts (plan-phone-interface.md), on both layouts: the notes a refused operation leaves, stacked at
 * the foot of the screen above the taskbar or the phone's bar, each going on its own. They take no press, so a
 * note never stands between the user and what is under it.
 */

import m from "mithril";
import type { Toast } from "../model/Toasts";

export interface ToastsAttrs {
  readonly toasts: readonly Toast[];
  /** Where the stack sits: fixed over the viewport above the taskbar, or inside the phone's layout (which follows
   *  the visual viewport, so a soft keyboard does not cover it) above its bar. */
  readonly placementClass: string;
}

export const Toasts: m.Component<ToastsAttrs> = {
  view(vnode) {
    const { toasts, placementClass } = vnode.attrs;
    if (toasts.length === 0) return null;
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
            class:
              "toast max-w-full animate-[phone-sheet-rise_var(--dur-slow)_ease-out] rounded-(--desk-toast-radius) " +
              "bg-inverse px-4 py-2.5 text-(length:--font-size-body) text-on-accent shadow-overlay",
          },
          toast.message,
        ),
      ),
    );
  },
};
