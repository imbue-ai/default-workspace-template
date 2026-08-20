import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mithril is mocked so the binding's redraw scheduling cannot escape the test.
const { mockRedraw } = vi.hoisted(() => ({ mockRedraw: vi.fn() }));
vi.mock("mithril", () => ({ default: { redraw: mockRedraw } }));

import { OVERSCAN_PX, createTranscriptVirtualizer, type TranscriptVirtualizer } from "./transcriptVirtualizer";

const VIEWPORT_HEIGHT = 500;
const ROW_ESTIMATE_PX = 100;

/**
 * A stand-in for the scroll container, exposing exactly the surface the library
 * reads. These run in vitest's node environment, where there is no DOM at all,
 * so the element is described rather than built: its size (which the library
 * takes from ``offsetWidth``/``offsetHeight``), the window it belongs to,
 * listener registration, and its scroll position.
 *
 * Two of those are load-bearing rather than incidental. ``scrollTo`` is the
 * library's *only* way of writing a scroll position -- it never assigns
 * ``scrollTop`` -- so an element without one cannot be moved, and a test
 * asserting that nothing moved it would pass on any code at all. And the scroll
 * listener is how an offset reaches the virtualizer, so capturing it is what
 * lets a test put the viewport anywhere but the top.
 *
 * There is no ResizeObserver here either, so the rect arrives once --
 * synchronously, when the element is first seen -- which is all these need.
 */
function fakeScrollElement(offsetHeight: number): { element: HTMLElement; fireScroll: () => void } {
  const scrollListeners: (() => void)[] = [];
  const element = {
    offsetWidth: 800,
    offsetHeight,
    scrollTop: 0,
    ownerDocument: { defaultView: { setTimeout: () => 0, clearTimeout: () => {} } },
    addEventListener: (type: string, handler: () => void) => {
      if (type === "scroll") {
        scrollListeners.push(handler);
      }
    },
    removeEventListener: () => {},
    scrollTo: ({ top }: { top: number }) => {
      element.scrollTop = top;
    },
  };
  return {
    element: element as unknown as HTMLElement,
    fireScroll: () => scrollListeners.forEach((handler) => handler()),
  };
}

interface Harness {
  virtualizer: TranscriptVirtualizer;
  element: HTMLElement;
  /** Push the current options and read the resulting window. */
  render: () => ReturnType<TranscriptVirtualizer["getVirtualItems"]>;
  setPinned: (indices: number[]) => void;
  /** Move the viewport as the user would: set the position, then let the
   *  element's own scroll event carry it to the virtualizer. */
  scrollTo: (top: number) => void;
}

function harness(
  options: {
    rowCount?: number;
    viewportHeight?: number;
    paddingStart?: number;
    paddingEnd?: number;
    enabled?: boolean;
  } = {},
): Harness {
  const rowCount = options.rowCount ?? 50;
  const { element, fireScroll } = fakeScrollElement(options.viewportHeight ?? VIEWPORT_HEIGHT);
  let pinned: number[] = [];
  const virtualizer = createTranscriptVirtualizer({
    getScrollElement: () => element,
    getCount: () => rowCount,
    getRowKey: (index) => `row-${index}`,
    estimateSize: () => ROW_ESTIMATE_PX,
    getPaddingStart: () => options.paddingStart ?? 0,
    getPaddingEnd: () => options.paddingEnd ?? 0,
    getPinnedIndices: () => pinned,
    isEnabled: () => options.enabled ?? true,
  });
  virtualizer.mount();
  return {
    virtualizer,
    element,
    setPinned: (indices: number[]) => (pinned = indices),
    scrollTo: (top: number) => {
      element.scrollTop = top;
      fireScroll();
    },
    render: () => {
      virtualizer.sync();
      return virtualizer.getVirtualItems();
    },
  };
}

function renderedIndices(harnessed: Harness): number[] {
  return harnessed.render().map((item) => item.index);
}

beforeEach(() => {
  mockRedraw.mockReset();
  vi.stubGlobal("requestAnimationFrame", () => 0);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createTranscriptVirtualizer windowing", () => {
  it("renders the viewport and its overscan rather than the whole transcript", () => {
    const indices = renderedIndices(harness({ rowCount: 200 }));

    expect(indices[0]).toBe(0);
    expect(indices).toContain(Math.floor(VIEWPORT_HEIGHT / ROW_ESTIMATE_PX));
    expect(indices.length).toBeLessThan(200);
  });

  it("takes a viewport size from an element that reports one even before the view is marked visible", () => {
    // A panel mounted as an inactive tab has no rect at all, and the resize it
    // gets when the tab is shown can land before the visibility attr does. A
    // virtualizer with no rect renders no rows and no spacers, and nothing
    // re-delivers it -- so the rect is judged on its own measurement, not on
    // what the view currently believes about itself.
    expect(renderedIndices(harness({ enabled: false }))).not.toEqual([]);
  });

  it("has no window while the scroll element reports no height", () => {
    // Which is what a hidden tab reports, and windowing against it would
    // recompute the range against a zero-height viewport.
    expect(renderedIndices(harness({ viewportHeight: 0 }))).toEqual([]);
  });

  it("ignores the scroll offset of a view that is not visible", () => {
    // The other half of the pair above, and deliberately not symmetric with it.
    // A zero rect says "hidden" on its own, but a zero offset is exactly what
    // the top of the transcript reads as -- so this one has to ask the view.
    // Letting a hidden tab's offset through would rewind its window to the top
    // and lose the place the reader had scrolled to.
    const harnessed = harness({ rowCount: 200, enabled: false });
    const before = renderedIndices(harnessed);

    harnessed.scrollTo(2000);

    expect(renderedIndices(harnessed)).toEqual(before);
  });

  it("follows the scroll offset of a view that is visible", () => {
    const harnessed = harness({ rowCount: 200 });
    const before = renderedIndices(harnessed);

    harnessed.scrollTo(2000);

    expect(renderedIndices(harnessed)).not.toEqual(before);
  });
});

// The pins are set before the first render throughout: the library memoizes the
// rendered index list on the range itself, so a pin that appears while nothing
// else moved is picked up on the next frame that changes the range -- which is
// every frame where the pin matters, since it exists for the viewport scrolling
// away from a selection.
describe("createTranscriptVirtualizer selection pinning", () => {
  it("keeps a selected row mounted without mounting everything between it and the viewport", () => {
    // Removing a selection endpoint's node collapses the selection, so the row
    // has to stay; mounting the run in between would make a distant selection
    // arbitrarily expensive, which is why the spacer bridges the gap instead.
    expect(renderedIndices(harness({ rowCount: 200 }))).not.toContain(150);

    const harnessed = harness({ rowCount: 200 });
    harnessed.setPinned([150]);
    const indices = renderedIndices(harnessed);

    expect(indices).toContain(150);
    expect(indices).not.toContain(100);
    // Still in order, so the renderer's running offset never goes backwards.
    expect([...indices].sort((a, b) => a - b)).toEqual(indices);
  });

  it("drops pinned indices that name no row", () => {
    const unpinned = renderedIndices(harness({ rowCount: 50 }));

    const harnessed = harness({ rowCount: 50 });
    harnessed.setPinned([-1, 50, 999]);

    expect(renderedIndices(harnessed)).toEqual(unpinned);
  });
});

describe("createTranscriptVirtualizer overscan", () => {
  it("covers a pixel budget rather than a fixed number of rows", () => {
    // A transcript row is anything from a one-line chip to a whole progress
    // block, so counting items would mean wildly different coverage depending
    // on what happens to be on screen.
    const shortRowPx = 40;
    const harnessed = harness({ rowCount: 400 });
    harnessed.render();
    for (let index = 0; index < 400; index++) {
      harnessed.virtualizer.resizeRow(index, shortRowPx);
    }

    const items = harnessed.render();
    const overscanBelowViewport = items[items.length - 1].end - VIEWPORT_HEIGHT;

    expect(overscanBelowViewport).toBeGreaterThanOrEqual(OVERSCAN_PX);
  });
});

describe("createTranscriptVirtualizer spacers", () => {
  it("brackets the rendered rows with the reserved space on both sides", () => {
    // The leading spacer is read straight off the first item's offset and the
    // trailing one off getTrailingSpace, so between them they have to account
    // for every row and both reserves.
    const paddingStart = 1000;
    const paddingEnd = 2000;
    const harnessed = harness({ rowCount: 10, paddingStart, paddingEnd });

    const items = harnessed.render();

    expect(items[0].start).toBe(paddingStart);
    expect(harnessed.virtualizer.getTrailingSpace()).toBe((10 - items.length) * ROW_ESTIMATE_PX + paddingEnd);
    expect(items[items.length - 1].end + harnessed.virtualizer.getTrailingSpace()).toBe(
      paddingStart + 10 * ROW_ESTIMATE_PX + paddingEnd,
    );
  });
});

describe("createTranscriptVirtualizer measurements", () => {
  it("never moves the scroll position when a measurement replaces an estimate", () => {
    // The view holds the reader's place by anchoring on the row being read,
    // which also covers the reserved space moving; a second writer of scrollTop
    // would double-correct. The row grown here sits entirely above the fold,
    // which is exactly the case the library compensates by default -- left
    // alone, it would push the viewport down by the 800px the estimate was out.
    const harnessed = harness({ rowCount: 50 });
    harnessed.render();
    harnessed.scrollTo(2000);
    harnessed.render();

    harnessed.virtualizer.resizeRow(0, 900);

    expect(harnessed.element.scrollTop).toBe(2000);
  });

  it("uses a measured height in place of the estimate", () => {
    const harnessed = harness({ rowCount: 50 });
    harnessed.render();

    harnessed.virtualizer.resizeRow(0, 350);

    expect(harnessed.render()[0].size).toBe(350);
  });

  it("forgets every measurement on reset", () => {
    // Switching to a different agent, or to a width whose heights are different
    // facts entirely.
    const harnessed = harness({ rowCount: 50 });
    harnessed.render();
    harnessed.virtualizer.resizeRow(0, 350);

    harnessed.virtualizer.reset();

    expect(harnessed.render()[0].size).toBe(ROW_ESTIMATE_PX);
  });
});
