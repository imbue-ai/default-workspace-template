/**
 * Pointer lock on the stage, for relative-motion programs on the Screen (spec section 12): the plumbing is here,
 * the control is hidden. neko v3's control events carry absolute positions only (``control/move`` is
 * ``{x, y}``; ``server/pkg/types/message/messages.go`` has no relative-motion message, and
 * ``server/internal/websocket/handler/control.go`` no handler for one), so the deltas a locked pointer yields have
 * nowhere faithful to go: integrating them into absolute moves breaks the moment a program warps the pointer
 * itself, which is what relative-motion programs do. The control shows once ``IS_RELATIVE_MOTION_SUPPORTED`` is
 * true, which a neko with a relative-move event makes so.
 */

export const IS_RELATIVE_MOTION_SUPPORTED = false;

export interface PointerLockOptions {
  /** The element the lock is requested on. */
  stage: HTMLElement;
  /** Relative motion while the pointer is locked, in CSS pixels. */
  onRelativeMove: (deltaX: number, deltaY: number) => void;
  /** The element the document says holds the lock, injectable for a document that has no pointer lock. */
  readLockedElement?: () => Element | null;
}

export class PointerLockControl {
  readonly button = document.createElement("button");
  private isLocked = false;

  constructor(private readonly options: PointerLockOptions) {
    this.button.className = "screen-button";
    this.button.textContent = "Lock pointer";
    this.button.dataset.screenPointerLock = "true";
    this.button.hidden = true;
    this.button.addEventListener("click", () => this.request());
    document.addEventListener("pointerlockchange", () => this.onLockChange());
    options.stage.addEventListener("mousemove", (event) => this.onMove(event));
  }

  /** Whether the stage holds the pointer lock now. */
  get locked(): boolean {
    return this.isLocked;
  }

  /** Offer the control while the human holds control, and only where the relative motion has somewhere to go. */
  setAvailable(isControlling: boolean): void {
    this.button.hidden = !(IS_RELATIVE_MOTION_SUPPORTED && isControlling);
  }

  private request(): void {
    void this.options.stage.requestPointerLock();
  }

  private lockedElement(): Element | null {
    return this.options.readLockedElement?.() ?? document.pointerLockElement ?? null;
  }

  /** Escape releases the lock in the browser itself; this only learns of it. */
  private onLockChange(): void {
    this.isLocked = this.lockedElement() === this.options.stage;
    this.button.textContent = this.isLocked ? "Pointer locked (Escape releases)" : "Lock pointer";
  }

  private onMove(event: MouseEvent): void {
    if (!this.isLocked) return;
    this.options.onRelativeMove(event.movementX, event.movementY);
  }
}
