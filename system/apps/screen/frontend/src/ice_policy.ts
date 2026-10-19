/** Direct first, then relay, then the JPEG screencast (docs/system/specs/screen/spec.md sections 5.3 and 5.4, the
 *  TURN spec section 4.4): an attempt that does not reach ``connected`` within four seconds, or fails later,
 *  escalates once to a relay restart (``connect-info?relay=1``, ICE restarted, the need remembered for next time);
 *  a relay attempt that fails the same way hands the picture to the screencast. From the screencast, WebRTC is tried
 *  again on a growing timer (and at once on the strip's Retry), each time with fresh relay servers, since the usual
 *  reason it failed is a relay credential that was not ready in time. */

import type { Scheduler } from "./scheduler";
import type { ConnectInfo } from "./state";

export const CONNECT_DEADLINE_MS = 4000;
/** How long the screencast waits before each WebRTC retry: the first, then doubling to the last, which repeats. */
export const SCREENCAST_RETRY_DELAYS_MS = [15000, 30000, 60000];

export type IcePhase = "idle" | "direct" | "relay" | "fetching-relay" | "connected" | "screencast";

export interface IcePolicyActions {
  /** ``connect-info?relay=1``. */
  fetchRelayInfo(): Promise<ConnectInfo>;
  /** Restart ICE with the servers and endpoints of ``info`` (null when the refetch failed: keep the current ones);
   *  the policy arms the relay attempt's deadline itself. */
  restartIce(info: ConnectInfo | null): void;
  /** No pair connected directly or through a relay: show the Screen another way. */
  fallbackToScreencast(): void;
  /** A retry from the screencast: a fresh peer with the servers and endpoints of ``info`` (null: the current ones),
   *  while the screencast keeps showing the Screen until the peer connects. */
  retryFromScreencast(info: ConnectInfo | null): void;
  /** Record that this client's network needed a relay. */
  rememberRelay(): void;
}

export class IcePolicy {
  phase: IcePhase = "idle";
  private deadline = 0;
  private retryTimer = 0;
  private retryCount = 0;
  private generation = 0;

  constructor(
    private readonly actions: IcePolicyActions,
    private readonly timers: Scheduler,
    private readonly deadlineMs: number = CONNECT_DEADLINE_MS,
    private readonly retryDelaysMs: readonly number[] = SCREENCAST_RETRY_DELAYS_MS,
  ) {}

  /** A peer connection was requested; ``isRelay`` says whether it already carries relay servers. */
  attemptStarted(isRelay: boolean): void {
    this.phase = isRelay ? "relay" : "direct";
    this.arm();
  }

  connected(): void {
    this.disarm();
    this.retryCount = 0;
    this.phase = "connected";
  }

  /** The browser reported the connection failed (or the server closed its side without a reconnect of its own). */
  failed(): void {
    if (this.phase === "idle" || this.phase === "screencast" || this.phase === "fetching-relay") return;
    this.disarm();
    this.escalate();
  }

  /** The user asked for another try from the screencast (the strip's Retry): now, and the timer starts over. */
  retryNow(): void {
    if (this.phase !== "screencast") return;
    this.clearRetry();
    this.retryCount = 0;
    this.retry();
  }

  /** The stream is over: nothing pending survives. */
  reset(): void {
    this.disarm();
    this.clearRetry();
    this.generation += 1;
    this.retryCount = 0;
    this.phase = "idle";
  }

  private arm(): void {
    this.disarm();
    this.deadline = this.timers.setTimeout(() => {
      this.deadline = 0;
      this.escalate();
    }, this.deadlineMs);
  }

  private disarm(): void {
    if (this.deadline !== 0) this.timers.clearTimeout(this.deadline);
    this.deadline = 0;
  }

  private clearRetry(): void {
    if (this.retryTimer !== 0) this.timers.clearTimeout(this.retryTimer);
    this.retryTimer = 0;
  }

  private escalate(): void {
    if (this.phase === "relay") {
      this.phase = "screencast";
      this.actions.fallbackToScreencast();
      this.scheduleRetry();
      return;
    }
    if (this.phase === "direct") this.actions.rememberRelay();
    // A connection that worked and then failed is retried through the relay too; it is not remembered as a relay
    // network, since the direct path did connect once.
    this.phase = "fetching-relay";
    const generation = this.generation;
    this.actions
      .fetchRelayInfo()
      .catch(() => null)
      .then((info) => {
        if (generation !== this.generation || this.phase !== "fetching-relay") return;
        this.actions.restartIce(info);
        this.attemptStarted(true);
      });
  }

  private scheduleRetry(): void {
    this.clearRetry();
    const delay = this.retryDelaysMs[Math.min(this.retryCount, this.retryDelaysMs.length - 1)];
    this.retryCount += 1;
    this.retryTimer = this.timers.setTimeout(() => {
      this.retryTimer = 0;
      this.retry();
    }, delay);
  }

  /** From the screencast: fresh relay servers, a fresh peer, the relay attempt's deadline; a failure lands back in
   *  the screencast with the next delay. */
  private retry(): void {
    this.phase = "fetching-relay";
    const generation = this.generation;
    this.actions
      .fetchRelayInfo()
      .catch(() => null)
      .then((info) => {
        if (generation !== this.generation || this.phase !== "fetching-relay") return;
        this.actions.retryFromScreencast(info);
        this.attemptStarted(true);
      });
  }
}
