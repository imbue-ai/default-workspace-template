/**
 * The shell's toasts (plan-phone-interface.md), on both layouts: the notes a refused operation leaves, stacked at
 * the foot of the screen above the taskbar or the phone's bar, each going on its own. They take no press, so a
 * note never stands between the user and what is under it.
 */

import m from "mithril";
import type { Toast } from "../model/Toasts";

export interface ToastsAttrs {
  readonly toasts: readonly Toast[];
  /** How far above the foot of the screen the stack sits: over the taskbar, or over the phone's bar. */
  readonly bottomClass: string;
}

export const Toasts: m.Component<ToastsAttrs> = {
  view(vnode) {
    const { toasts, bottomClass } = vnode.attrs;
    if (toasts.length === 0) return null;
    return m(
      "div",
      {
        role: "status",
        "aria-live": "polite",
        class:
          `toasts pointer-events-none fixed inset-x-0 z-(--z-overlay) flex flex-col items-center gap-2 px-4 ` +
          bottomClass,
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
