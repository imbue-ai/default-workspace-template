/**
 * A page's toasts: a failed operation says so in a short note that rises at the foot of the page and goes away on
 * its own, instead of a browser ``alert()`` that stops everything until it is dismissed. ``components/toasts.ts``
 * draws them.
 */

/** What a toast is about: a plain note, or a status the brand tints (a failure is ``danger``). */
export type ToastTone = "default" | "info" | "success" | "warn" | "danger";

export interface Toast {
  readonly id: number;
  readonly message: string;
  readonly tone: ToastTone;
}

/** How long a toast stays up. */
export const TOAST_DURATION_MS = 5000;

/** The toasts on screen, oldest first; each leaves after ``durationMs``, or sooner when dismissed. */
export class ToastQueue {
  private toasts: readonly Toast[] = [];
  private nextId = 1;

  constructor(
    private readonly onChange: () => void,
    private readonly durationMs: number = TOAST_DURATION_MS,
  ) {}

  show(message: string, tone: ToastTone = "default"): void {
    const toast: Toast = { id: this.nextId, message, tone };
    this.nextId += 1;
    this.toasts = [...this.toasts, toast];
    setTimeout(() => this.dismiss(toast.id), this.durationMs);
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
