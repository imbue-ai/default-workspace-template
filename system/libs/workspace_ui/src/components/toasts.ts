/**
 * The toasts of ``models/toasts.ts``, stacked at the foot of the page. The stack is a live region that stays mounted
 * while empty: a screen reader announces a note added to a region it already knows, not one that arrives with it.
 *
 * A plain toast goes on its own and, without details, takes no press unless the page also sets ``closesPlainToasts``:
 * the shell's never stand between the user and what is under them, while a page whose notes run longer (Files lists
 * what failed) gives each a close button. A plain toast with details takes presses either way, for its "See details"
 * and close button; opening the details holds it up (``onHold``). A lasting toast (one that stays until dismissed)
 * takes presses and carries a close control wherever the page offers ``onDismiss``. A lasting toast whose message
 * asks for a bug report gets "Ask an agent to fix this" while the page gives ``onAskAgent`` (unless the toast withholds
 * it) and "Report a bug" while the page gives ``onReportBug``, each decided as it draws: whether a shell frames the page
 * and whether the embedder opens a bug report can be learned after the toast went up.
 */

import m from "mithril";

import { askAgentToFix } from "../app_contract";
import type { ReportedFailure, ShellConnection } from "../app_contract";
import { REPORT_A_BUG_LABEL } from "../models/failure";
import type { Toast, ToastAction } from "../models/toasts";
import { Button } from "./Button";
import { ASK_AGENT_TO_FIX_LABEL, ASKING_AN_AGENT_LABEL } from "./FailureButtons";
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
  /** Open the bug report about a reportable toast's failure; absent or null where none can be opened now. */
  readonly onReportBug?: ((failure: ReportedFailure) => void) | null;
  /** Ask an agent to fix a reportable toast's failure, settling once the ask has; absent or null where no agent can
   *  be asked now (``askAgentFromToasts``). */
  readonly onAskAgent?: ((failure: ReportedFailure) => Promise<unknown>) | null;
}

/** The ``onAskAgent`` of a framed page's toasts: the toast's failure sent to an agent through the shell, which shows
 *  the chat beside the page's window and toasts a refused ask itself; null where no shell frames the page. */
export function askAgentFromToasts(
  connection: Pick<ShellConnection, "hasShell" | "askAgent"> | null,
): ((failure: ReportedFailure) => Promise<unknown>) | null {
  if (connection === null || !connection.hasShell()) return null;
  return (failure) => askAgentToFix(connection, { whatFailed: failure.message, details: failure.details });
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

/** The asks a ``Toasts`` stack has out, by toast id: each reads "Asking an agent…" until it settles. */
type AskingToasts = Set<number>;

function lastingToastBody(
  toast: Toast,
  onDismiss: ((id: number) => void) | null,
  onReportBug: ((failure: ReportedFailure) => void) | null,
  onAskAgent: ((failure: ReportedFailure) => Promise<unknown>) | null,
  asking: AskingToasts,
): m.Children {
  const failure: ReportedFailure = { message: toast.message, details: toast.details };
  const askAgent: ToastAction | null =
    toast.isAgentOffered && onAskAgent !== null
      ? {
          label: ASK_AGENT_TO_FIX_LABEL,
          run: () => {
            if (asking.has(toast.id)) return;
            asking.add(toast.id);
            void onAskAgent(failure).finally(() => {
              asking.delete(toast.id);
              m.redraw();
            });
          },
          keepsToast: true,
          pending: { isPending: () => asking.has(toast.id), label: ASKING_AN_AGENT_LABEL },
        }
      : null;
  const reportBug: ToastAction | null =
    toast.isReportable && onReportBug !== null
      ? { label: REPORT_A_BUG_LABEL, run: () => onReportBug(failure), keepsToast: true }
      : null;
  const remedies = [askAgent, reportBug].filter((action): action is ToastAction => action !== null);
  const actions = [...toast.actions, ...remedies];
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
    actions.length === 0
      ? null
      : m(
          "div",
          { class: "toast-actions -mx-2 flex flex-wrap gap-1" },
          actions.map((action) => {
            const isPending = action.pending?.isPending() === true;
            return m(
              Button,
              {
                variant: "ghost-inverse",
                sm: true,
                extra:
                  action === reportBug
                    ? "toast-action report-bug"
                    : action === askAgent
                      ? "toast-action ask-agent-to-fix"
                      : "toast-action",
                disabled: isPending,
                // A retry or a resolving button has done the note's job, and a retry that fails again leaves a new
                // one; a button that hands the failure on leaves it up for its other buttons.
                onclick: () => {
                  if (action.keepsToast !== true) onDismiss?.(toast.id);
                  action.run();
                },
              },
              isPending ? action.pending?.label : action.label,
            );
          }),
        ),
  ];
}

export const Toasts: m.ClosureComponent<ToastsAttrs> = () => {
  const asking: AskingToasts = new Set();
  return {
    view(vnode) {
      const { toasts, placementClass, onDismiss, closesPlainToasts, onHold, onReportBug, onAskAgent } = vnode.attrs;
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
              ? lastingToastBody(toast, onDismiss ?? null, onReportBug ?? null, onAskAgent ?? null, asking)
              : plainToastBody(toast, plainDismiss(toast), onHold ?? null),
          ),
        ),
      );
    },
  };
};
