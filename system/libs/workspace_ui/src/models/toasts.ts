/**
 * A page's toasts: a failed operation says so in a short note that rises at the foot of the page, instead of a
 * browser ``alert()`` that stops everything until it is dismissed. ``components/toasts.ts`` draws them. The caller
 * says which kind it shows: a plain note, which goes away on its own and is for a failure whose retry is the control
 * just pressed (it may carry details, and stays once they are opened), or a lasting one, which stays until dismissed
 * with its details and buttons, for a failure whose state outlasts five seconds.
 */

import type { AgentFixRequest } from "../app_contract";

export interface ToastAction {
  readonly label: string;
  readonly run: () => void;
  /** Leave the toast up once pressed: a button that hands the failure to someone else ("Ask an agent to ..."), after
   *  which the toast's other buttons (Try again) still apply. A retry or a button that resolves the failure leaves
   *  this unset, and the toast goes. */
  readonly keepsToast?: boolean;
  /** While ``isPending()`` (read as the toast draws), the button is disabled under ``label`` ("Asking an agent…"):
   *  what it started has not settled yet. */
  readonly pending?: { readonly isPending: () => boolean; readonly label: string };
}

export type ToastOptions =
  | {
      readonly isLasting: false;
      /** What goes behind "See details"; none when null or absent. */
      readonly details?: string | null;
    }
  | {
      readonly isLasting: true;
      /** What goes behind "See details"; none when null or absent. */
      readonly details?: string | null;
      /** Buttons after the message (Try again, Ask an agent to ...), in order. */
      readonly actions?: readonly ToastAction[];
      /** The failure owes the person help: the toast offers "Ask an agent to fix this" after its buttons wherever
       *  the page can ask one when the toast is drawn, and "Report a bug" where it can only open one
       *  (``components/toasts.ts``'s ``onAskAgent`` and ``onReportBug``), which may be only after the toast went up. */
      readonly isHelpOwed?: boolean;
      /** No agent is offered for this failure, even where the page can ask one: the ask would go through what
       *  failed (the chat app's own server not answering), so it could not reach an agent. */
      readonly isAgentWithheld?: boolean;
      /** What the toast's agent is asked, where that is more than the toast's own message and details: what the
       *  person was trying to do, an instruction other than to find and fix a fault. */
      readonly agentRequest?: AgentFixRequest;
      /** No bug report is offered for this failure, even where the page can open one: a user's own app caused it,
       *  and its faults are not Imbue's bugs. */
      readonly isBugReportWithheld?: boolean;
    };

export interface Toast {
  readonly id: number;
  readonly message: string;
  readonly details: string | null;
  readonly actions: readonly ToastAction[];
  readonly isLasting: boolean;
  /** A lasting toast whose failure owes the person help; never a plain one. */
  readonly isHelpOwed: boolean;
  /** A toast owing help that offers an agent where the page can ask one (``isAgentWithheld`` unset). */
  readonly isAgentOffered: boolean;
  /** What its agent is asked: the caller's ``agentRequest``, else its message and details. */
  readonly agentRequest: AgentFixRequest;
  /** A toast owing help that offers "Report a bug" where the page can open one (``isBugReportWithheld`` unset). */
  readonly isBugReportOffered: boolean;
}

/** How long a plain toast stays up. */
export const TOAST_DURATION_MS = 5000;

/** How many lasting toasts stay on screen at once; a newer one drops the oldest. */
const MAX_LASTING_TOASTS = 3;

/** The toasts on screen, oldest first; a plain one leaves after ``durationMs``, every one when dismissed. A toast
 *  that says the same as one already up (message and details) replaces it rather than stacking. */
export class ToastQueue {
  private toasts: readonly Toast[] = [];
  private nextId = 1;
  private readonly leaveTimers = new Map<number, ReturnType<typeof setTimeout>>();

  constructor(
    private readonly onChange: () => void,
    private readonly durationMs: number = TOAST_DURATION_MS,
  ) {}

  show(message: string, options: ToastOptions = { isLasting: false }): void {
    const details = options.details ?? null;
    const actions = options.isLasting ? (options.actions ?? []) : [];
    const isHelpOwed = options.isLasting && options.isHelpOwed === true;
    const isAgentOffered = isHelpOwed && options.isAgentWithheld !== true;
    const isBugReportOffered = isHelpOwed && options.isBugReportWithheld !== true;
    const toast: Toast = {
      id: this.nextId,
      message,
      details,
      actions,
      isLasting: options.isLasting,
      isHelpOwed,
      isAgentOffered,
      agentRequest: (options.isLasting ? options.agentRequest : undefined) ?? { whatFailed: message, details },
      isBugReportOffered,
    };
    this.nextId += 1;
    let kept = this.toasts.filter((other) => other.message !== message || other.details !== details);
    if (toast.isLasting) {
      const lasting = kept.filter((other) => other.isLasting);
      const overflow = Math.max(0, lasting.length - (MAX_LASTING_TOASTS - 1));
      const dropped = new Set(lasting.slice(0, overflow).map((other) => other.id));
      kept = kept.filter((other) => !dropped.has(other.id));
    }
    this.toasts = [...kept, toast];
    if (!toast.isLasting) {
      this.leaveTimers.set(
        toast.id,
        setTimeout(() => this.dismiss(toast.id), this.durationMs),
      );
    }
    this.onChange();
  }

  /** Keep a plain note up until it is dismissed: its reader opened its details. */
  hold(id: number): void {
    clearTimeout(this.leaveTimers.get(id));
    this.leaveTimers.delete(id);
  }

  dismiss(id: number): void {
    this.hold(id);
    if (!this.toasts.some((toast) => toast.id === id)) return;
    this.toasts = this.toasts.filter((toast) => toast.id !== id);
    this.onChange();
  }

  current(): readonly Toast[] {
    return this.toasts;
  }
}
