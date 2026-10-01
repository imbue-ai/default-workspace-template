/**
 * The shell's toasts (plan-phone-interface.md): a failed operation says so in a short note that rises over the
 * desktop or above the phone's bar and goes away on its own, instead of a browser ``alert()`` that stops
 * everything until it is dismissed.
 */

export interface Toast {
  readonly id: number;
  readonly message: string;
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

  show(message: string): void {
    const toast: Toast = { id: this.nextId, message };
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
