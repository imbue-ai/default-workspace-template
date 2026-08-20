import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril is mocked so the measure -> redraw scheduling can be asserted without
// a real DOM or render cycle.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { redraw: mockRedraw } }));

import {
  MEASURE_HYSTERESIS_PX,
  SETTLE_QUIET_MS,
  createRowMeasureScheduler,
  createRowMeasurementStore,
  measureMountedRows,
} from "./rowMeasurement";

/** A store whose clock the test drives, so settling is deterministic. */
function storeWithClock(): { store: ReturnType<typeof createRowMeasurementStore>; advance: (ms: number) => void } {
  let clock = 1000;
  const store = createRowMeasurementStore(() => clock);
  return { store, advance: (ms: number) => (clock += ms) };
}

describe("createRowMeasurementStore hysteresis", () => {
  it("accepts the first measurement", () => {
    const { store } = storeWithClock();
    expect(store.observe("a", 120.4)).toBe(120.4);
    expect(store.heightFor("a")).toBe(120.4);
  });

  it("returns the anchored height for a sub-pixel change", () => {
    // The exact loop being broken: a row at a fractional vertical offset reflows
    // by a fraction each frame. Returning the accepted value rather than the
    // observed one keeps the cache anchored, so the redraw it would otherwise
    // schedule never happens.
    const { store } = storeWithClock();
    store.observe("a", 100);
    expect(store.observe("a", 100.6)).toBe(100);
    expect(store.observe("a", 99.5)).toBe(100);
    expect(store.heightFor("a")).toBe(100);
  });

  it("ignores a delta of exactly the threshold and accepts one just past it", () => {
    const { store } = storeWithClock();
    store.observe("a", 100);
    expect(store.observe("a", 100 + MEASURE_HYSTERESIS_PX)).toBe(100);
    expect(store.observe("a", 100 + MEASURE_HYSTERESIS_PX + 0.01)).toBe(100 + MEASURE_HYSTERESIS_PX + 0.01);
  });

  it("never ratchets across the threshold under repeated wobble", () => {
    // Each step is sub-threshold but they drift upward. Anchoring to the
    // accepted value (not the previous observation) is what stops the cache
    // walking across the threshold one fraction at a time.
    const { store } = storeWithClock();
    store.observe("a", 100);
    for (const height of [100.6, 99.5, 100.7, 99.6, 100.8]) {
      expect(store.observe("a", height)).toBe(100);
    }
    expect(store.heightFor("a")).toBe(100);
  });

  it("accepts a genuine content change well past the threshold", () => {
    const { store } = storeWithClock();
    store.observe("a", 100);
    expect(store.observe("a", 122)).toBe(122);
  });

  it("keeps the prior height when a row transiently reads zero", () => {
    // A hidden or not-yet-laid-out row reads as zero; recording that would
    // collapse the geometry for the whole conversation.
    const { store } = storeWithClock();
    store.observe("a", 310);
    expect(store.observe("a", 0)).toBe(310);
    expect(store.heightFor("a")).toBe(310);
  });

  it("tracks rows independently", () => {
    const { store } = storeWithClock();
    store.observe("a", 100);
    store.observe("b", 400);
    expect(store.observe("a", 100.4)).toBe(100);
    expect(store.heightFor("b")).toBe(400);
  });
});

describe("createRowMeasurementStore settling", () => {
  it("does not consider a freshly measured row settled", () => {
    const { store } = storeWithClock();
    store.observe("a", 100);
    expect(store.isSettled("a", 1000)).toBe(false);
  });

  it("settles a row once it has gone quiet for the settle window", () => {
    // Markdown, highlighting and images all land after first paint, so the
    // height at mount is routinely not the final height.
    const { store, advance } = storeWithClock();
    store.observe("a", 100);
    advance(SETTLE_QUIET_MS);
    expect(store.isSettled("a", 1000 + SETTLE_QUIET_MS)).toBe(true);
  });

  it("restarts the settle window when the height changes again", () => {
    const { store, advance } = storeWithClock();
    store.observe("a", 100);
    advance(SETTLE_QUIET_MS - 1);
    // A late-loading image grows the row just before it would have settled.
    store.observe("a", 400);
    expect(store.isSettled("a", 1000 + SETTLE_QUIET_MS - 1)).toBe(false);
  });

  it("does not restart the settle window for a sub-threshold observation", () => {
    // Wobble must not keep a row perpetually unsettled, or nothing would ever
    // be persisted on a page that jitters slightly.
    const { store, advance } = storeWithClock();
    store.observe("a", 100);
    advance(SETTLE_QUIET_MS - 1);
    store.observe("a", 100.4);
    advance(1);
    expect(store.isSettled("a", 1000 + SETTLE_QUIET_MS)).toBe(true);
  });
});

describe("createRowMeasurementStore bookkeeping", () => {
  it("keeps entries until the store drifts well past the live set", () => {
    const { store } = storeWithClock();
    for (let i = 0; i < 300; i++) {
      store.observe(`row-${i}`, 100);
    }
    // 300 cached against 250 live is within the slack -- nothing is dropped.
    const live = new Set(Array.from({ length: 250 }, (_, i) => `row-${i}`));
    store.prune(live);
    expect(store.heightFor("row-299")).toBe(100);
  });

  it("drops stale entries once the drift is large enough", () => {
    const { store } = storeWithClock();
    for (let i = 0; i < 300; i++) {
      store.observe(`row-${i}`, 100);
    }
    store.prune(new Set(["row-0"]));
    expect(store.heightFor("row-0")).toBe(100);
    expect(store.heightFor("row-299")).toBeUndefined();
  });

  it("forgets everything on reset", () => {
    const { store } = storeWithClock();
    store.observe("a", 100);
    store.reset();
    expect(store.heightFor("a")).toBeUndefined();
  });
});

/** A minimal stand-in for the rendered list: children with ids and heights. */
function listWith(rows: Array<{ id: string; height: number }>): Element {
  return {
    children: rows.map((row) => ({
      id: row.id,
      getBoundingClientRect: () => ({ height: row.height }) as DOMRect,
    })),
  } as unknown as Element;
}

describe("measureMountedRows", () => {
  it("measures every identified row and reports the changes", () => {
    const { store } = storeWithClock();
    const changed = measureMountedRows(
      listWith([
        { id: "a", height: 100 },
        { id: "b", height: 240 },
      ]),
      store,
    );
    expect([...changed.entries()]).toEqual([
      ["a", 100],
      ["b", 240],
    ]);
  });

  it("skips spacers, which carry no id", () => {
    const { store } = storeWithClock();
    const changed = measureMountedRows(
      listWith([
        { id: "", height: 5000 },
        { id: "a", height: 100 },
      ]),
      store,
    );
    expect([...changed.keys()]).toEqual(["a"]);
  });

  it("reports nothing when every row is unchanged", () => {
    const { store } = storeWithClock();
    const list = listWith([{ id: "a", height: 100 }]);
    measureMountedRows(list, store);
    expect(measureMountedRows(list, store).size).toBe(0);
  });

  it("reports nothing for sub-threshold drift", () => {
    // The measure -> redraw -> reflow -> measure loop is broken here too: a
    // sub-pixel change must not report as a change, or it schedules the redraw
    // that shifts the row again.
    const { store } = storeWithClock();
    measureMountedRows(listWith([{ id: "a", height: 100 }]), store);
    expect(measureMountedRows(listWith([{ id: "a", height: 100.6 }]), store).size).toBe(0);
  });

  it("skips rows that are not laid out", () => {
    const { store } = storeWithClock();
    const changed = measureMountedRows(listWith([{ id: "a", height: 0 }]), store);
    expect(changed.size).toBe(0);
    expect(store.heightFor("a")).toBeUndefined();
  });
});

describe("createRowMeasureScheduler", () => {
  let frames: Array<() => void>;

  beforeEach(() => {
    frames = [];
    mockRedraw.mockReset();
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.push(() => callback(0));
      return frames.length;
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** A scheduler over one row whose measured height the test can move. */
  function schedulerOverRow(): {
    scheduler: ReturnType<typeof createRowMeasureScheduler>;
    store: ReturnType<typeof createRowMeasurementStore>;
    reported: Array<[string, number]>;
    setHeight: (height: number) => void;
    hideList: () => void;
  } {
    const { store } = storeWithClock();
    const reported: Array<[string, number]> = [];
    let height = 100;
    let hasList = true;
    return {
      store,
      reported,
      setHeight: (next: number) => (height = next),
      hideList: () => (hasList = false),
      scheduler: createRowMeasureScheduler({
        store,
        getListElement: () => (hasList ? listWith([{ id: "a", height }]) : null),
        reportHeight: (rowKey, measured) => reported.push([rowKey, measured]),
      }),
    };
  }

  it("defers the pass to the next frame, then reports and redraws once", () => {
    const { scheduler, reported } = schedulerOverRow();

    scheduler.schedule();
    expect(mockRedraw).not.toHaveBeenCalled();
    frames.forEach((run) => run());

    expect(reported).toEqual([["a", 100]]);
    expect(mockRedraw).toHaveBeenCalledTimes(1);
  });

  it("debounces repeated calls into a single frame", () => {
    // A global redraw fires on every scroll tick and every streamed event, and
    // reading layout is not free; one pass per frame is the whole point.
    const { scheduler } = schedulerOverRow();

    scheduler.schedule();
    scheduler.schedule();
    scheduler.schedule();

    expect(frames).toHaveLength(1);
  });

  it("schedules again once the frame has run", () => {
    // The flag has to clear before the frame's early returns, or one pass with
    // nothing to measure would wedge every later schedule.
    const { scheduler, hideList } = schedulerOverRow();
    hideList();

    scheduler.schedule();
    frames.forEach((run) => run());
    scheduler.schedule();

    expect(frames).toHaveLength(2);
  });

  it("neither reports nor redraws when no height moved", () => {
    const { scheduler, reported } = schedulerOverRow();
    scheduler.schedule();
    frames.forEach((run) => run());
    mockRedraw.mockReset();

    scheduler.schedule();
    frames[1]();

    expect(reported).toEqual([["a", 100]]);
    expect(mockRedraw).not.toHaveBeenCalled();
  });

  it("neither reports nor redraws for sub-threshold drift", () => {
    // The end-to-end guarantee: even while the view keeps scheduling passes, a
    // row wobbling sub-pixel never triggers the redraw that would move it again.
    const { scheduler, setHeight } = schedulerOverRow();
    scheduler.schedule();
    frames.forEach((run) => run());
    mockRedraw.mockReset();

    setHeight(100 + MEASURE_HYSTERESIS_PX);
    scheduler.schedule();
    frames[1]();

    expect(mockRedraw).not.toHaveBeenCalled();
  });

  it("reads nothing when there is no list to measure", () => {
    // Not mounted yet, or a hidden panel whose rows are not laid out.
    const { scheduler, reported, hideList } = schedulerOverRow();
    hideList();

    scheduler.schedule();
    frames.forEach((run) => run());

    expect(reported).toEqual([]);
    expect(mockRedraw).not.toHaveBeenCalled();
  });
});
