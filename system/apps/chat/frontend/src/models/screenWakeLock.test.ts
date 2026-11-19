import { describe, expect, it } from "vitest";
import { browserWakeLockHost, createScreenWakeLock } from "./screenWakeLock";
import type { WakeLockHost, WakeLockSentinelLike } from "./screenWakeLock";

class FakeSentinel implements WakeLockSentinelLike {
  released = false;
  releaseCalls = 0;
  releaseFails = false;

  release(): Promise<void> {
    this.releaseCalls += 1;
    this.released = true;
    return this.releaseFails ? Promise.reject(new Error("already released")) : Promise.resolve();
  }

  /** The browser let go of the lock on its own, as it does when the page is hidden. */
  dropByBrowser(): void {
    this.released = true;
  }
}

interface PendingRequest {
  resolve(sentinel: FakeSentinel): void;
  reject(error: Error): void;
}

class FakeHost implements WakeLockHost {
  visible = true;
  readonly requests: PendingRequest[] = [];
  private readonly listeners = new Set<() => void>();

  request(): Promise<WakeLockSentinelLike> {
    return new Promise((resolve, reject) => this.requests.push({ resolve, reject }));
  }

  isVisible(): boolean {
    return this.visible;
  }

  onVisibilityChange(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    for (const listener of this.listeners) listener();
  }

  /** Grant the oldest request still waiting. */
  grant(): FakeSentinel {
    const sentinel = new FakeSentinel();
    this.requests.shift()!.resolve(sentinel);
    return sentinel;
  }

  /** Refuse the oldest request still waiting, as a browser does on low battery or without the grant. */
  refuse(): void {
    this.requests.shift()!.reject(new Error("NotAllowedError"));
  }
}

const settled = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe("the screen wake lock", () => {
  it("acquires the lock when wanted and releases it when not", async () => {
    const host = new FakeHost();
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    expect(host.requests).toHaveLength(1);
    const sentinel = host.grant();
    await settled();
    expect(sentinel.releaseCalls).toBe(0);
    lock.setWanted(false);
    expect(sentinel.releaseCalls).toBe(1);
  });

  it("does nothing on a repeated call with the same value", async () => {
    const host = new FakeHost();
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    lock.setWanted(true);
    expect(host.requests).toHaveLength(1);
    const sentinel = host.grant();
    await settled();
    lock.setWanted(false);
    lock.setWanted(false);
    expect(sentinel.releaseCalls).toBe(1);
  });

  it("requests nothing while the page is hidden, and only once while a request is in flight", () => {
    const host = new FakeHost();
    host.visible = false;
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    expect(host.requests).toHaveLength(0);
    host.setVisible(true);
    expect(host.requests).toHaveLength(1);
    // Another visibility event while the first request is still unanswered asks for nothing more.
    host.setVisible(true);
    expect(host.requests).toHaveLength(1);
  });

  it("requests the lock again when the page comes back, after the browser let it go", async () => {
    const host = new FakeHost();
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    const first = host.grant();
    await settled();
    // Coming back while the lock is still held asks for nothing.
    host.setVisible(true);
    expect(host.requests).toHaveLength(0);
    first.dropByBrowser();
    host.setVisible(false);
    expect(host.requests).toHaveLength(0);
    host.setVisible(true);
    expect(host.requests).toHaveLength(1);
    const second = host.grant();
    await settled();
    lock.setWanted(false);
    expect(second.releaseCalls).toBe(1);
    // Once unwanted, the page coming back asks for nothing.
    host.setVisible(true);
    expect(host.requests).toHaveLength(0);
  });

  it("releases a grant that arrives after the lock stopped being wanted", async () => {
    const host = new FakeHost();
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    lock.setWanted(false);
    const sentinel = host.grant();
    await settled();
    expect(sentinel.releaseCalls).toBe(1);
  });

  it("swallows a refusal, a failing release, and a missing API", async () => {
    const host = new FakeHost();
    const lock = createScreenWakeLock(host);
    lock.setWanted(true);
    host.refuse();
    await settled();
    // A refusal leaves the lock wanting; the page coming back tries again.
    host.setVisible(true);
    expect(host.requests).toHaveLength(1);
    const sentinel = host.grant();
    sentinel.releaseFails = true;
    await settled();
    lock.setWanted(false);
    await settled();
    expect(sentinel.releaseCalls).toBe(1);
    // No API at all: nothing to do, nothing thrown.
    const none = createScreenWakeLock(null);
    none.setWanted(true);
    none.setWanted(false);
  });

  it("has no browser host in a window-less environment", () => {
    expect(browserWakeLockHost()).toBeNull();
  });
});
