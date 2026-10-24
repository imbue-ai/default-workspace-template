import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DashboardModel, OUTAGE_GRACE_MS, type StreamLike } from "./dashboard";

class FakeStream implements StreamLike {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  addEventListener(): void {}
  close(): void {}
}

/** A model whose every read reaches only the tunnel, which answers 503, and whose streams are fakes it opens. */
function unreachableModel(): { model: DashboardModel; streams: FakeStream[] } {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 503, headers: { "Content-Type": "application/json" } })),
  );
  const streams: FakeStream[] = [];
  const model = new DashboardModel({
    route: { page: "home" },
    now: () => new Date(),
    redraw: () => {},
    openStream: () => {
      const stream = new FakeStream();
      streams.push(stream);
      return stream;
    },
  });
  return { model, streams };
}

describe("DashboardModel's grace for a brief outage", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("calls the collector down only once it has stayed unreachable for the grace period", async () => {
    const { model } = unreachableModel();

    await model.refresh();
    expect(model.isCollectorDown).toBe(false);

    vi.advanceTimersByTime(OUTAGE_GRACE_MS - 1_000);
    await model.refresh();
    expect(model.isCollectorDown).toBe(false);

    vi.advanceTimersByTime(1_000);
    await model.refresh();
    expect(model.isCollectorDown).toBe(true);
    model.stop();
  });

  it("keeps showing live through a stream drop shorter than the grace period", async () => {
    const { model, streams } = unreachableModel();
    model.start();
    streams[0].onopen?.(new Event("open"));
    expect(model.isLive).toBe(true);

    streams[0].onerror?.(new Event("error"));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(model.isLive).toBe(true);
    streams[streams.length - 1].onopen?.(new Event("open"));

    await vi.advanceTimersByTimeAsync(OUTAGE_GRACE_MS);
    expect(model.isLive).toBe(true);
    model.stop();
  });

  it("shows the stream down once it has stayed down for the grace period", async () => {
    const { model, streams } = unreachableModel();
    model.start();
    streams[0].onopen?.(new Event("open"));

    streams[0].onerror?.(new Event("error"));
    await vi.advanceTimersByTimeAsync(OUTAGE_GRACE_MS - 1);
    expect(model.isLive).toBe(true);

    await vi.advanceTimersByTimeAsync(1);
    expect(model.isLive).toBe(false);
    model.stop();
  });
});
