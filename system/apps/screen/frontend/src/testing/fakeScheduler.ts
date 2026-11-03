/** Timers a test advances by hand, in the shape the viewer's clocks take. */

import type { Scheduler } from "../scheduler";

interface PendingTimer {
  readonly handle: number;
  readonly dueAt: number;
  readonly callback: () => void;
}

export class FakeScheduler implements Scheduler {
  now = 0;
  private nextHandle = 1;
  private pending: PendingTimer[] = [];

  setTimeout(callback: () => void, delayMs: number): number {
    const handle = this.nextHandle++;
    this.pending.push({ handle, dueAt: this.now + delayMs, callback });
    return handle;
  }

  clearTimeout(handle: number): void {
    this.pending = this.pending.filter((timer) => timer.handle !== handle);
  }

  /** How many timers are armed and not yet due. */
  get pendingCount(): number {
    return this.pending.length;
  }

  /** Move the clock forward, running every timer that comes due in order. */
  advance(ms: number): void {
    const target = this.now + ms;
    let due = this.pending.filter((timer) => timer.dueAt <= target).sort((a, b) => a.dueAt - b.dueAt);
    while (due.length > 0) {
      const timer = due[0];
      this.now = timer.dueAt;
      this.pending = this.pending.filter((other) => other.handle !== timer.handle);
      timer.callback();
      due = this.pending.filter((other) => other.dueAt <= target).sort((a, b) => a.dueAt - b.dueAt);
    }
    this.now = target;
  }
}
