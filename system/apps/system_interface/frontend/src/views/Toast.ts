/**
 * The shell's toasts (plan-phone-interface.md), on both layouts: the notes a refused operation leaves, stacked at
 * the foot of the screen above the taskbar or the phone's bar. A plain note goes on its own and takes no press, so it
 * never stands between the user and what is under it; a lasting one (details or buttons) takes presses and carries a
 * close control. The stack is a live region that stays mounted while empty: a screen reader announces a note added to
 * a region it already knows, not one that arrives with it.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { SeeDetails } from "@imbue/workspace-ui/src/components/SeeDetails";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { Toast } from "../model/Toasts";

const CLOSE_GLYPH_SIZE = 14;

export interface ToastsAttrs {
  readonly toasts: readonly Toast[];
  /** Where the stack sits: fixed over the viewport above the taskbar, or inside the phone's layout (which follows
   *  the visual viewport, so a soft keyboard does not cover it) above its bar. */
  readonly placementClass: string;
  readonly onDismiss: (id: number) => void;
}

function lastingToastBody(toast: Toast, onDismiss: (id: number) => void): m.Children {
  return [
    m("div", { class: "toast-head flex items-start gap-2" }, [
      m("span", { class: "toast-message min-w-0 flex-1 py-0.5" }, toast.message),
      m(
        Button,
        {
          variant: "ghost-inverse",
          sm: true,
          icon: true,
          extra: "toast-close -my-1 -mr-2 flex-none",
          "aria-label": "Dismiss",
          onclick: () => onDismiss(toast.id),
        },
        m.trust(icon("close", { size: CLOSE_GLYPH_SIZE })),
      ),
    ]),
    m(SeeDetails, { details: toast.details }),
    toast.actions.length === 0
      ? null
      : m(
          "div",
          { class: "toast-actions -mx-2 flex flex-wrap gap-1" },
          toast.actions.map((action) =>
            m(
              Button,
              {
                variant: "ghost-inverse",
                sm: true,
                extra: "toast-action",
                // The note has done its job once its next step is taken; a retry that fails again leaves a new one.
                onclick: () => {
                  onDismiss(toast.id);
                  action.run();
                },
              },
              action.label,
            ),
          ),
        ),
  ];
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
            class:
              (toast.isLasting
                ? "toast toast--lasting pointer-events-auto flex w-[min(28rem,100%)] flex-col gap-1.5 "
                : "toast ") +
              "max-w-full animate-[phone-sheet-rise_var(--dur-slow)_ease-out] rounded-(--desk-toast-radius) " +
              "bg-inverse px-4 py-2.5 text-(length:--font-size-body) text-on-accent shadow-overlay",
          },
          toast.isLasting ? lastingToastBody(toast, onDismiss) : toast.message,
        ),
      ),
    );
  },
};
