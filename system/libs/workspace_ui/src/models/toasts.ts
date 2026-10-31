/**
 * A page's toasts: a failed operation says so in a short note that rises at the foot of the page, instead of a
 * browser ``alert()`` that stops everything until it is dismissed. ``components/toasts.ts`` draws them. The caller
 * says which kind it shows: a plain note, which goes away on its own and is for a failure whose retry is the control
 * just pressed, or a lasting one, which stays until dismissed with its details and buttons, for a failure whose state
 * outlasts five seconds.
 */

export interface ToastAction {
  readonly label: string;
  readonly run: () => void;
}

export type ToastOptions =
  | { readonly isLasting: false }
  | {
      readonly isLasting: true;
      /** What goes behind "See details"; none when null or absent. */
      readonly details?: string | null;
      /** Buttons after the message (Try again, Report a bug, ...), in order. */
      readonly actions?: readonly ToastAction[];
    };

export interface Toast {
  readonly id: number;
  readonly message: string;
  readonly details: string | null;
  readonly actions: readonly ToastAction[];
  readonly isLasting: boolean;
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

  constructor(
    private readonly onChange: () => void,
    private readonly durationMs: number = TOAST_DURATION_MS,
  ) {}

  show(message: string, options: ToastOptions = { isLasting: false }): void {
    const details = options.isLasting ? (options.details ?? null) : null;
    const actions = options.isLasting ? (options.actions ?? []) : [];
    const toast: Toast = { id: this.nextId, message, details, actions, isLasting: options.isLasting };
    this.nextId += 1;
    let kept = this.toasts.filter((other) => other.message !== message || other.details !== details);
    if (toast.isLasting) {
      const lasting = kept.filter((other) => other.isLasting);
      const overflow = Math.max(0, lasting.length - (MAX_LASTING_TOASTS - 1));
      const dropped = new Set(lasting.slice(0, overflow).map((other) => other.id));
      kept = kept.filter((other) => !dropped.has(other.id));
    }
    this.toasts = [...kept, toast];
    if (!toast.isLasting) setTimeout(() => this.dismiss(toast.id), this.durationMs);
    this.onChange();
  }

  dismiss(id: number): void {
    if (!this.toasts.some((toast) => toast.id === id)) return;
    this.toasts = this.toasts.filter((toast) => toast.id !== id);
    this.onChange();
  }

  current(): readonly Toast[] {
    return this.toasts;
  }
}
