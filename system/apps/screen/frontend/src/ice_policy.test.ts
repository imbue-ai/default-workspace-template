/** The direct-then-relay-then-screencast state machine over a fake clock and fake actions. */
import { describe, expect, it } from "vitest";

import { CONNECT_DEADLINE_MS, IcePolicy, SCREENCAST_RETRY_DELAYS_MS } from "./ice_policy";
import type { IcePolicyActions } from "./ice_policy";
import type { ConnectInfo } from "./state";
import { FakeScheduler } from "./testing/fakeScheduler";

const RELAY_INFO: ConnectInfo = {
  ice_servers: [{ urls: "turn:turn.example:3478", username: "u", credential: "c" }],
  candidates: [{ host: "127.0.0.1", port: 32771, protocol: "udp" }],
  expires_at: "2026-10-17T13:00:00Z",
};

class Recorder implements IcePolicyActions {
  readonly log: string[] = [];
  restarted: (ConnectInfo | null)[] = [];
  fetchResult: Promise<ConnectInfo> = Promise.resolve(RELAY_INFO);

  fetchRelayInfo(): Promise<ConnectInfo> {
    this.log.push("fetch");
    return this.fetchResult;
  }

  restartIce(info: ConnectInfo | null): void {
    this.log.push("restart");
    this.restarted.push(info);
  }

  fallbackToScreencast(): void {
    this.log.push("screencast");
  }

  retryFromScreencast(info: ConnectInfo | null): void {
    this.log.push("retry");
    this.restarted.push(info);
  }

  rememberRelay(): void {
    this.log.push("remember");
  }
}

async function settle(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

function build(): { policy: IcePolicy; clock: FakeScheduler; actions: Recorder } {
  const clock = new FakeScheduler();
  const actions = new Recorder();
  return { policy: new IcePolicy(actions, clock), clock, actions };
}

describe("IcePolicy", () => {
  it("escalates a direct attempt that misses the four-second deadline to a remembered relay restart", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(false);
    clock.advance(CONNECT_DEADLINE_MS - 1);
    expect(actions.log).toEqual([]);
    clock.advance(1);
    await settle();
    expect(actions.log).toEqual(["remember", "fetch", "restart"]);
    expect(actions.restarted).toEqual([RELAY_INFO]);
    expect(policy.phase).toBe("relay");
    expect(clock.pendingCount).toBe(1);
  });

  it("hands a relay attempt that misses the deadline to the screencast", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    expect(policy.phase).toBe("screencast");
    // The retry timer is the one thing armed.
    expect(clock.pendingCount).toBe(1);
  });

  it("tries WebRTC again from the screencast on a growing timer, with fresh relay servers each time", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0] - 1);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    clock.advance(1);
    await settle();
    expect(actions.log).toEqual(["screencast", "fetch", "retry"]);
    expect(actions.restarted).toEqual([RELAY_INFO]);
    expect(policy.phase).toBe("relay");
    // The retry misses its deadline: back to the screencast, with the next, longer delay.
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["screencast", "fetch", "retry", "screencast"]);
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[1] - 1);
    await settle();
    expect(actions.log.length).toBe(4);
    clock.advance(1);
    await settle();
    expect(actions.log.slice(4)).toEqual(["fetch", "retry"]);
    // The last delay repeats.
    for (let round = 0; round < 3; round++) {
      clock.advance(CONNECT_DEADLINE_MS);
      await settle();
      clock.advance(SCREENCAST_RETRY_DELAYS_MS[SCREENCAST_RETRY_DELAYS_MS.length - 1] - 1);
      await settle();
      const before = actions.log.length;
      clock.advance(1);
      await settle();
      expect(actions.log.slice(before)).toEqual(["fetch", "retry"]);
    }
  });

  it("a retry that connects ends the screencast and starts the delays over on the next fall", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0]);
    await settle();
    policy.connected();
    expect(policy.phase).toBe("connected");
    expect(clock.pendingCount).toBe(0);
    policy.failed();
    await settle();
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log.slice(-1)).toEqual(["screencast"]);
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0]);
    await settle();
    expect(actions.log.slice(-2)).toEqual(["fetch", "retry"]);
  });

  it("Retry now tries at once from the screencast and starts the delays over, and does nothing elsewhere", async () => {
    const { policy, clock, actions } = build();
    policy.retryNow();
    expect(actions.log).toEqual([]);
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0]);
    await settle();
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["screencast", "fetch", "retry", "screencast"]);
    policy.retryNow();
    await settle();
    expect(actions.log.slice(4)).toEqual(["fetch", "retry"]);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0]);
    await settle();
    expect(actions.log.slice(6)).toEqual(["screencast", "fetch", "retry"]);
  });

  it("an attempt the stream starts on its own from the screencast drops the pending retry", async () => {
    // The server closed a retry's peer and the stream asked for a fresh one 1.5 s later, after the deadline had
    // already fallen the policy back to the screencast.
    const { policy, clock, actions } = build();
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    policy.attemptStarted(true);
    expect(clock.pendingCount).toBe(1);
    policy.connected();
    expect(clock.pendingCount).toBe(0);
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[SCREENCAST_RETRY_DELAYS_MS.length - 1]);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    expect(policy.phase).toBe("connected");
  });

  it("a reset from the screencast drops the pending retry", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    policy.reset();
    expect(clock.pendingCount).toBe(0);
    clock.advance(SCREENCAST_RETRY_DELAYS_MS[0]);
    await settle();
    expect(actions.log).toEqual(["screencast"]);
    expect(policy.phase).toBe("idle");
  });

  it("walks direct, relay, screencast when nothing ever connects", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(false);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["remember", "fetch", "restart", "screencast"]);
  });

  it("a connection in time disarms the deadline", () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(false);
    clock.advance(1000);
    policy.connected();
    clock.advance(CONNECT_DEADLINE_MS);
    expect(actions.log).toEqual([]);
    expect(policy.phase).toBe("connected");
  });

  it("a failure reported by the browser escalates at once", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(false);
    clock.advance(500);
    policy.failed();
    await settle();
    expect(actions.log).toEqual(["remember", "fetch", "restart"]);
    expect(policy.phase).toBe("relay");
  });

  it("a connection that fails later is retried through the relay without being remembered", async () => {
    const { policy, clock, actions } = build();
    policy.attemptStarted(false);
    policy.connected();
    policy.failed();
    await settle();
    expect(actions.log).toEqual(["fetch", "restart"]);
    clock.advance(CONNECT_DEADLINE_MS);
    expect(actions.log).toEqual(["fetch", "restart", "screencast"]);
  });

  it("a failed refetch still restarts ICE with the endpoints already held", async () => {
    const { policy, clock, actions } = build();
    actions.fetchResult = Promise.reject(new Error("connect-info answered 503"));
    policy.attemptStarted(false);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    expect(actions.log).toEqual(["remember", "fetch", "restart"]);
    expect(actions.restarted).toEqual([null]);
  });

  it("a reset while the relay info is in flight drops the restart", async () => {
    const { policy, clock, actions } = build();
    let resolve: (info: ConnectInfo) => void = () => undefined;
    actions.fetchResult = new Promise((r) => {
      resolve = r;
    });
    policy.attemptStarted(false);
    clock.advance(CONNECT_DEADLINE_MS);
    policy.reset();
    resolve(RELAY_INFO);
    await settle();
    expect(actions.log).toEqual(["remember", "fetch"]);
    expect(policy.phase).toBe("idle");
  });

  it("ignores failures reported while idle, fetching or already on the screencast", async () => {
    const { policy, clock, actions } = build();
    policy.failed();
    expect(actions.log).toEqual([]);
    policy.attemptStarted(true);
    clock.advance(CONNECT_DEADLINE_MS);
    await settle();
    policy.failed();
    expect(actions.log).toEqual(["screencast"]);
  });
});
