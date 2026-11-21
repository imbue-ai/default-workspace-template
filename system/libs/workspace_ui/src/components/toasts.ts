/**
 * The toasts of ``models/toasts.ts``, stacked at the foot of the page. The stack is a live region that stays mounted
 * while empty: a screen reader announces a note added to a region it already knows, not one that arrives with it.
 *
 * A plain toast goes on its own and, without details, takes no press unless the page also sets ``closesPlainToasts``:
 * the shell's never stand between the user and what is under them, while a page whose notes run longer (Files lists
 * what failed) gives each a close button. A plain toast with details takes presses either way, for its "See details"
 * and close button; opening the details holds it up (``onHold``). A lasting toast (one that stays until dismissed)
 * takes presses and carries a close control wherever the page offers ``onDismiss``.
 */

import m from "mithril";

import type { Toast } from "../models/toasts";
import { Button } from "./Button";
import { SeeDetails } from "./SeeDetails";
import { icon } from "./icons";

const CLOSE_GLYPH_SIZE = 14;

export interface ToastsAttrs {
  readonly toasts: readonly Toast[];
  /** Where the stack sits: ``fixed`` over the viewport, or ``absolute`` inside a layout, and how far up. */
  readonly placementClass: string;
  /** Dismiss the toast with this id: a lasting toast's close control and its buttons call it. */
  readonly onDismiss?: (id: number) => void;
  /** Give each plain toast a close button too (it calls ``onDismiss``). */
  readonly closesPlainToasts?: boolean;
  /** Keep a plain toast up until dismissed: its reader opened its details. */
  readonly onHold?: (id: number) => void;
}

function plainToastBody(
  toast: Toast,
  onDismiss: ((id: number) => void) | null,
  onHold: ((id: number) => void) | null,
): m.Children {
  const head = plainToastHead(toast, onDismiss);
  if (toast.details === null) return head;
  return [
    m("div", { class: "flex items-start gap-3" }, head),
    m(SeeDetails, { details: toast.details, onOpen: () => onHold?.(toast.id) }),
  ];
}

function plainToastHead(toast: Toast, onDismiss: ((id: number) => void) | null): m.Children {
  return [
    m("span", { class: "min-w-0 flex-1" }, toast.message),
    onDismiss === null
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
          m.trust(icon("close", { size: CLOSE_GLYPH_SIZE })),
        ),
  ];
}

function lastingToastBody(toast: Toast, onDismiss: ((id: number) => void) | null): m.Children {
  return [
    m("div", { class: "toast-head flex items-start gap-2" }, [
      m("span", { class: "toast-message min-w-0 flex-1 py-0.5" }, toast.message),
      onDismiss === null
        ? null
        : m(
            Button,
            {
              variant: "ghost-inverse",
              sm: true,
              icon: true,
              extra: "toast-close -my-1 -mr-2 flex-none",
              "aria-label": "Dismiss",
              "data-toast-dismiss": "",
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
                // A retry or a resolving button has done the note's job, and a retry that fails again leaves a new
                // one.
                onclick: () => {
                  onDismiss?.(toast.id);
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
    const { toasts, placementClass, onDismiss, closesPlainToasts, onHold } = vnode.attrs;
    const plainDismiss = (toast: Toast): ((id: number) => void) | null =>
      onDismiss !== undefined && (closesPlainToasts === true || toast.details !== null) ? onDismiss : null;
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
              (toast.isLasting
                ? "toast toast--lasting pointer-events-auto flex w-[min(28rem,100%)] flex-col gap-1.5 "
                : toast.details !== null
                  ? "toast pointer-events-auto flex flex-col gap-1.5 "
                  : `toast flex items-start gap-3 ${plainDismiss(toast) === null ? "" : "pointer-events-auto "}`) +
              "max-w-[min(480px,100%)] animate-[toast-rise_var(--dur-slow)_ease-out] rounded-[20px] bg-inverse " +
              "px-4 py-2.5 text-(length:--font-size-body) whitespace-pre-line text-on-accent shadow-overlay",
          },
          toast.isLasting
            ? lastingToastBody(toast, onDismiss ?? null)
            : plainToastBody(toast, plainDismiss(toast), onHold ?? null),
        ),
      ),
    );
  },
};
