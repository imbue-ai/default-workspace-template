/**
 * The controller's stage size, reported to the service debounced (docs/system/specs/screen/spec.md section 7.2).
 * Only a change from the last report is sent, and only while enabled (this session controls, the stack runs, and
 * this is not a phone). The latest measurement is kept across disabled spells and goes out as soon as the reporter
 * is enabled, whether or not it matches the last report (the Screen may have been sized by someone else meanwhile);
 * ``forget`` makes the next size go out regardless, for when the stage was laid out again.
 */

import type { Scheduler } from "./scheduler";

export const RESIZE_DEBOUNCE_MS = 300;
/** A stage this small is collapsed (a hidden window's frame) and reports nothing. */
const MIN_REPORTABLE_PX = 64;

export interface StageSize {
  width: number;
  height: number;
}

export class ResizeReporter {
  private isEnabled = false;
  /** The stage's latest measurement, kept across disabled spells so taking control reports the current size. */
  private latest: StageSize | null = null;
  private lastSent: StageSize | null = null;
  private timer: number | null = null;

  constructor(
    private readonly send: (size: StageSize) => void,
    private readonly scheduler: Scheduler,
    private readonly debounceMs = RESIZE_DEBOUNCE_MS,
  ) {}

  setEnabled(isEnabled: boolean): void {
    const wasEnabled = this.isEnabled;
    this.isEnabled = isEnabled;
    if (!isEnabled) {
      this.cancel();
    } else if (!wasEnabled) {
      this.lastSent = null;
      if (this.latest !== null) this.schedule();
    }
  }

  /** A new stage measurement; the send waits for the debounce window to pass without another. */
  observe(width: number, height: number): void {
    const size = { width: Math.round(width), height: Math.round(height) };
    if (size.width < MIN_REPORTABLE_PX || size.height < MIN_REPORTABLE_PX) return;
    this.latest = size;
    if (this.isEnabled) this.schedule();
  }

  /** Report the size again on the next measurement even when it matches the last one sent. */
  forget(): void {
    this.lastSent = null;
  }

  private schedule(): void {
    this.cancel();
    this.timer = this.scheduler.setTimeout(() => {
      this.timer = null;
      this.flush();
    }, this.debounceMs);
  }

  private cancel(): void {
    if (this.timer !== null) {
      this.scheduler.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private flush(): void {
    const size = this.latest;
    if (size === null || !this.isEnabled) return;
    if (this.lastSent !== null && this.lastSent.width === size.width && this.lastSent.height === size.height) return;
    this.lastSent = size;
    this.send(size);
  }
}
