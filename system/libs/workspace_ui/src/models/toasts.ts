/**
 * A page's toasts: a failed operation says so in a short note that rises at the foot of the page, instead of a
 * browser ``alert()`` that stops everything until it is dismissed. ``components/toasts.ts`` draws them. The caller
 * says which kind it shows: a plain note, which goes away on its own and is for a failure whose retry is the control
 * just pressed (it may carry details, and stays once they are opened), or a lasting one, which stays until dismissed
 * with its details and buttons, for a failure whose state outlasts five seconds.
 */

export interface ToastAction {
  readonly label: string;
  readonly run: () => void;
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
      /** Buttons after the message (Try again, ...), in order. */
      readonly actions?: readonly ToastAction[];
      /** The message asks for a bug report: the toast offers "Report a bug" after its buttons wherever the page can
       *  open one when the toast is drawn (``components/toasts.ts``'s ``onReportBug``), which may be only after the
       *  toast went up. */
      readonly isReportable?: boolean;
    };

export interface Toast {
  readonly id: number;
  readonly message: string;
  readonly details: string | null;
  readonly actions: readonly ToastAction[];
  readonly isLasting: boolean;
  /** A lasting toast whose message asks for a bug report; never a plain one. */
  readonly isReportable: boolean;
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
    const isReportable = options.isLasting && options.isReportable === true;
    const toast: Toast = { id: this.nextId, message, details, actions, isLasting: options.isLasting, isReportable };
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
