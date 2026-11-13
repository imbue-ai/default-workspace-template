/**
 * The toasts of ``models/toasts.ts``, stacked at the foot of the page, each going on its own. The stack is a live
 * region that stays mounted while empty: a screen reader announces a note added to a region it already knows, not
 * one that arrives with it.
 *
 * A toast takes no press unless the page offers ``onDismiss``: the shell's never stand between the user and what is
 * under them, while a page whose notes run longer (Files lists what failed) gives each a close button.
 */

import m from "mithril";

import type { Toast, ToastTone } from "../models/toasts";
import { icon } from "./icons";

// A plain note is the dark chip; a status is its tint one step deeper than a notice's, in the status's text.
const TOAST_TONES: Record<ToastTone, string> = {
  default: "bg-inverse-surface text-inverse-content",
  info: "bg-accent-4 text-accent-subtle-content",
  success: "bg-success-4 text-success-subtle-content",
  warn: "bg-warn-4 text-warn-subtle-content",
  danger: "bg-danger-4 text-danger-subtle-content",
};

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
            "data-tone": toast.tone,
            class:
              "toast flex max-w-[min(480px,100%)] animate-[toast-rise_var(--dur-slow)_ease-out] items-start gap-3 " +
              "rounded-xl px-4 py-2.5 text-(length:--font-size-body) whitespace-pre-line shadow-overlay " +
              `${TOAST_TONES[toast.tone]} ${onDismiss === undefined ? "" : "pointer-events-auto"}`,
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
                    class: "flex h-5 shrink-0 cursor-pointer items-center opacity-70 hover:opacity-100",
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
