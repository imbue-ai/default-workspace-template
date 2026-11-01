/**
 * Whether this viewer should hold media (docs/system/specs/screen/spec.md section 8.2): visible when the document is
 * not hidden, the shell said shown (or never said hidden), the stage intersects the viewport, and the stage is laid
 * out. Hidden for the hide delay, the media is released; visible again, it is requested at once. The inputs are fed
 * by the composition in ``index.ts`` (``visibilitychange``, the shell's messages, an IntersectionObserver, a 1.5 s
 * belt that owns the laid-out input alone, so a frame the observer missed is caught and let go by the same hand);
 * this is the state machine over them.
 */

import type { Scheduler } from "./scheduler";

export const HIDE_DELAY_MS = 5000;
export const REEVALUATE_INTERVAL_MS = 1500;

export interface VisibilityCallbacks {
  /** The viewer has been hidden for the delay: close the media. */
  onRelease: () => void;
  /** The viewer is visible again after a release (or for the first time): request the media. Answers whether it
   *  was asked for; a request that could not be made (no stream, no state, no socket yet) leaves the viewer wanting
   *  media, so the next evaluation asks again instead of waiting for another hide and show. */
  onRequest: () => boolean;
}

export class ViewerVisibility {
  private isDocumentHidden = false;
  private isShellShown = true;
  private isIntersecting = true;
  private isLaidOut = true;
  private hasMedia = false;
  private hideTimer: number | null = null;

  constructor(
    private readonly callbacks: VisibilityCallbacks,
    private readonly scheduler: Scheduler,
    private readonly hideDelayMs = HIDE_DELAY_MS,
  ) {}

  /** Whether the viewer is visible right now. */
  get isVisible(): boolean {
    return !this.isDocumentHidden && this.isShellShown && this.isIntersecting && this.isLaidOut;
  }

  /** Whether the viewer holds (or has asked for) media. */
  get wantsMedia(): boolean {
    return this.hasMedia;
  }

  setDocumentHidden(isHidden: boolean): void {
    this.isDocumentHidden = isHidden;
    this.evaluate();
  }

  setShellShown(isShown: boolean): void {
    this.isShellShown = isShown;
    this.evaluate();
  }

  setIntersecting(isIntersecting: boolean): void {
    this.isIntersecting = isIntersecting;
    this.evaluate();
  }

  /** Whether the stage has a size on screen (the belt's reading of its rectangle). */
  setLaidOut(isLaidOut: boolean): void {
    this.isLaidOut = isLaidOut;
    this.evaluate();
  }

  /** Re-read the inputs: the belt calls this on a cadence, every setter calls it at once. */
  evaluate(): void {
    if (this.isVisible) {
      if (this.hideTimer !== null) {
        this.scheduler.clearTimeout(this.hideTimer);
        this.hideTimer = null;
      }
      if (!this.hasMedia) this.hasMedia = this.callbacks.onRequest();
    } else if (this.hasMedia && this.hideTimer === null) {
      this.hideTimer = this.scheduler.setTimeout(() => {
        this.hideTimer = null;
        this.hasMedia = false;
        this.callbacks.onRelease();
      }, this.hideDelayMs);
    }
  }

  /** The media went away for another reason (the stack stopped): the next visible evaluation requests again. */
  reset(): void {
    if (this.hideTimer !== null) {
      this.scheduler.clearTimeout(this.hideTimer);
      this.hideTimer = null;
    }
    this.hasMedia = false;
  }
}
