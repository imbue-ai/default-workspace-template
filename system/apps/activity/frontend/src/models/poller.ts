/**
 * A read repeated on an interval while started. Each start is a generation: a read still in flight from an earlier
 * one finishes, but schedules nothing, so stopping and starting again quickly never leaves two loops running.
 */

export interface Poller {
  start(): void;
  stop(): void;
}

/** ``read`` resolves whether to keep reading; false ends the loop until the next start. */
export function createPoller(read: () => Promise<boolean>, intervalMs: number): Poller {
  let generation = 0;
  let isActive = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  function readThenSchedule(startedGeneration: number): void {
    void read().then((isContinuing) => {
      if (!isActive || startedGeneration !== generation || !isContinuing) return;
      timer = setTimeout(() => readThenSchedule(startedGeneration), intervalMs);
    });
  }

  return {
    start() {
      if (isActive) return;
      isActive = true;
      generation += 1;
      readThenSchedule(generation);
    },
    stop() {
      isActive = false;
      generation += 1;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
