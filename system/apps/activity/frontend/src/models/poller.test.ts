import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createPoller } from "./poller";

/** A read that stays in flight until the test settles it, counting how often it was started. */
function controlledRead(): {
  read: () => Promise<boolean>;
  settle: (isContinuing: boolean) => void;
  count: () => number;
} {
  const pending: ((isContinuing: boolean) => void)[] = [];
  return {
    read: () => new Promise<boolean>((resolve) => pending.push(resolve)),
    settle: (isContinuing) => pending.shift()?.(isContinuing),
    count: () => pending.length,
  };
}

describe("the poller", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("reads at once and then every interval until stopped", async () => {
    let reads = 0;
    const poller = createPoller(async () => {
      reads += 1;
      return true;
    }, 1000);
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(reads).toBe(1);
    await vi.advanceTimersByTimeAsync(3000);
    expect(reads).toBe(4);
    poller.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(reads).toBe(4);
  });

  it("keeps one loop when stopped and started again while a read is still in flight", async () => {
    const controlled = controlledRead();
    let started = 0;
    const poller = createPoller(() => {
      started += 1;
      return controlled.read();
    }, 1000);
    poller.start();
    poller.stop();
    poller.start();
    expect(started).toBe(2);
    controlled.settle(true);
    controlled.settle(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(started).toBe(3);
    controlled.settle(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(started).toBe(4);
  });

  it("stops reading when a read says there is no point going on", async () => {
    let reads = 0;
    const poller = createPoller(async () => {
      reads += 1;
      return false;
    }, 1000);
    poller.start();
    await vi.advanceTimersByTimeAsync(5000);
    expect(reads).toBe(1);
  });
});
