/**
 * The DOM-side transcript scroll engine (spec:
 * docs/system/specs/transcript-smooth-scroll.md). One instance per transcript
 * view. It owns:
 *
 *  - the two state machines (scroll position, scrollbar interaction), fed ONLY
 *    genuine user input: every programmatic scrollTop write is echo-tracked and
 *    its scroll event consumed before reduction -- the no-jitter guarantee;
 *  - per-redraw positioning: FOLLOW pins the bottom, USER_CONTROLLED re-derives
 *    scrollTop from the anchor row's measured position, which is also what
 *    absorbs every height change above the anchor (measurements landing,
 *    backfill pages, spacer re-estimates) with zero visible motion;
 *  - the physical layer's height table (live rows measured on a rAF, off-window
 *    rows measured offscreen in idle batches) and its geometry;
 *  - virtual end-spacer sizing (smoothed physical average, frozen while the
 *    user holds the scrollbar);
 *  - progressive fill: one planner action in flight at a time against the data
 *    source (grow / jump / evict), focused on where the user is;
 *  - scroll-state persistence per agent, and the ?debug=scroll trace.
 *
 * Native wheel/touch/keyboard scrolling stays native: the engine never writes
 * scrollTop in response to it, it only observes. The native scrollbar is hidden
 * (see style.css); TranscriptScrollbar renders the custom one from this
 * engine's mapping.
 */

import m from "mithril";
import {
  FILL_CHUNK_LIMIT,
  INITIAL_TAIL_LIMIT,
  JUMP_WINDOW_LIMIT,
  PHYSICAL_CAP_EVENTS,
  planNextFill,
  type FillAction,
  type FillFocus,
} from "../models/transcriptScroll/fillPlanner";
import {
  anchorFromViewport,
  buildPhysicalGeometry,
  computeVisibleRowRange,
  rowIndexOfKey,
  scrollTopForAnchor,
  type VisibleRowRange,
} from "../models/transcriptScroll/geometry";
import {
  decodePersistedScrollState,
  encodePersistedScrollState,
  FOLLOW_RESTORED,
  scrollStateStorageKey,
  validateRestoredAnchor,
  type RestoredScrollState,
} from "../models/transcriptScroll/persistence";
import { buildRowEventIndexes, rowIndexForEventIndex } from "../models/transcriptScroll/rowEventIndex";
import {
  computeLiveMapping,
  computeThumb,
  mappingPhysicalExtent,
  resolveTrackFraction,
  resolveTrackFractionToIndex,
} from "../models/transcriptScroll/scrollbarMap";
import {
  computeObservedPxPerEvent,
  computeSpacerUpdate,
  DEFAULT_SPACER_PX_PER_EVENT,
  SPACER_SMOOTHING_ALPHA,
} from "../models/transcriptScroll/spacerEstimate";
import {
  ELSEWHERE_STATE,
  FOLLOW_STATE,
  reduceScrollPosition,
  reduceScrollbarInteraction,
} from "../models/transcriptScroll/state";
import { createScrollTrace, type ScrollTrace } from "../models/transcriptScroll/trace";
import type {
  PhysicalExtent,
  PhysicalGeometry,
  ScrollAnchor,
  ScrollbarMapping,
  ScrollInputSource,
  ScrollPositionEvent,
  ScrollPositionState,
  Viewport,
} from "../models/transcriptScroll/types";
import { isSelectionActiveWithin, selectionStateWithin } from "./scroll-selection";
import { createOffscreenMeasurer } from "./offscreen-measure";
import type { RowDescriptor } from "./conversation-rows";

// Pixels rendered above/below the viewport so scrolling does not flash blank
// before the next redraw fills the window.
export const OVERSCAN_PX = 800;
// Within this many pixels of the bottom counts as "all the way down".
const BOTTOM_THRESHOLD_PX = 40;
// A measured height must differ by more than this to count as a change
// (breaks the sub-pixel measure->redraw->reflow feedback loop).
const MEASURE_HYSTERESIS_PX = 1;
// A scroll event within this distance of a pending programmatic write is that
// write's echo, not user input. Tight on purpose: echoes carry the exact
// read-back value, and a loose tolerance would eat the first tiny wheel-up
// events of a gesture while the streaming pin keeps echoes pending -- the
// "stuck at the bottom" feel.
const ECHO_TOLERANCE_PX = 0.25;
// Debounce for persisting scroll state to localStorage.
const PERSIST_DEBOUNCE_MS = 300;
// The scrollbar is shown for this long after scroll/drag activity (plus while hovered).
export const SCROLLBAR_SHOW_MS = 1200;
// Large enough to reconstruct a multi-second interaction afterwards; debug-only
// (the ?debug=scroll ring), so memory is a non-issue.
const TRACE_CAPACITY = 8000;

export interface TranscriptScrollDataSource {
  /** Rows derived from the loaded window, memoized by the caller per render version. */
  getRows(): RowDescriptor[];
  /** Event ids of the loaded window, in order (engine caches per render version). */
  getWindowEventIds(): readonly string[];
  getFirstOffset(): number;
  /** Server total, or null before the first window has been placed. */
  getTotalEvents(): number | null;
  getRenderVersion(): number;
  /** Execute one planner action; resolves when it has landed (or failed). */
  executeFill(action: FillAction): Promise<void>;
}

export interface TranscriptScrollEngineConfig {
  dataSource: TranscriptScrollDataSource;
  /** Defaults to always-visible (SubagentView); ChatPanel feeds dockview's tab visibility. */
  isVisible?: () => boolean;
  /**
   * Whether the agent is mid-generation. While FOLLOW, the bottom pin only
   * pulls the viewport down when this is true (or history is still filling in):
   * an idle transcript relayout (expanding a block at the tail) must not drag
   * the user. Defaults to true.
   */
  isStreaming?: () => boolean;
}

export interface TranscriptRenderPlan {
  /** Top spacer: the virtual end spacer plus the unmounted rows above the window. */
  topPadPx: number;
  /** Bottom spacer: the unmounted rows below the window plus the virtual end spacer. */
  bottomPadPx: number;
  startIndex: number;
  endIndex: number;
}

export interface ScrollbarRenderState {
  thumbStartFraction: number;
  thumbSizeFraction: number;
  /** Whether recent activity should keep the scrollbar visible (hover adds to this). */
  isActive: boolean;
  hasTrack: boolean;
}

export interface TranscriptScrollEngine {
  /** Compute this frame's spacers + mounted row range. Call from view() after rows are built. */
  computeRenderPlan(): TranscriptRenderPlan;
  /** Attach listeners and apply positioning. Call from oncreate/onupdate with the scroll element. */
  afterRender(element: HTMLElement): void;
  /** Tear down listeners/observers/timers. Call from onremove. */
  detach(): void;
  /** Reset all state for a different agent, loading its persisted scroll state. */
  setAgent(agentKey: string | null): void;
  /** The user submitted a message: snap back to following the tail. */
  noteMessageSent(): void;
  /** Viewport currently sits over a virtual end spacer (show the loading overlay). */
  isViewportInSpacer(): boolean;

  // Custom scrollbar contract (see TranscriptScrollbar).
  scrollbarEngage(): void;
  scrollbarMoveTo(fraction: number): void;
  /** Pointer released: the thumb goes back to rendering the LIVE viewport
   *  position (the frozen mapping stays for future scrubs). While dragging the
   *  thumb tracks the pointer; after release that would freeze it wherever the
   *  pointer let go -- showing "at the bottom" while the content is not. */
  scrollbarRelease(): void;
  getScrollbarRenderState(): ScrollbarRenderState;
}

function isTraceEnabled(): boolean {
  if (typeof location !== "undefined" && location.search.includes("debug=scroll")) {
    return true;
  }
  // The embedded pane inside the desktop client has no query string to carry
  // the flag; localStorage lets debugging be switched on there (takes effect
  // on the next pane load).
  try {
    return localStorage.getItem("transcript-scroll-debug") === "1";
  } catch {
    return false;
  }
}

export function createTranscriptScrollEngine(config: TranscriptScrollEngineConfig): TranscriptScrollEngine {
  const dataSource = config.dataSource;
  const isVisible = config.isVisible ?? (() => true);
  const isStreaming = config.isStreaming ?? (() => true);

  // --- state machines -------------------------------------------------------
  let positionState: ScrollPositionState = FOLLOW_STATE;
  let scrollbarState = ELSEWHERE_STATE;

  // --- geometry / heights ---------------------------------------------------
  const heightByRowKey = new Map<string, number>();
  let geometry: PhysicalGeometry | null = null;
  let geometryRows: RowDescriptor[] = [];
  let rowEventIndexes: number[] = [];
  let cachedWindowEventIds: readonly string[] = [];
  let cachedRenderVersion = -1;
  let heightsEpoch = 0;
  let geometryHeightsEpoch = -1;
  // Append tracking for the FOLLOW pin: the loaded window's end index at the
  // last rows refresh, and whether events appended since the last pin.
  let lastSeenEndIndex = -1;
  let hasUnfollowedAppend = false;

  // --- spacers --------------------------------------------------------------
  let estimatePxPerEvent = DEFAULT_SPACER_PX_PER_EVENT;
  let spacerTopPx = 0;
  let spacerBottomPx = 0;

  // --- DOM / viewport -------------------------------------------------------
  let scrollEl: HTMLElement | null = null;
  let scrollTopPx = 0;
  let viewportHeightPx = 0;
  let lastListWidthPx: number | null = null;
  let resizeObserver: ResizeObserver | null = null;
  // Observes the message list's box: content can resize outside any redraw
  // (an image finishing loading, a late font swap), which would leave FOLLOW
  // unpinned or an anchor unheld until something else redraws. A redraw runs
  // afterRender, which repositions.
  let listResizeObserver: ResizeObserver | null = null;
  let observedListEl: Element | null = null;
  let pointerReleaseListener: (() => void) | null = null;
  let isPointerDown = false;
  // A genuine downward user scroll (or a scrollbar move targeting the very
  // bottom) while the fill still lagged the server total: complete the exact
  // bottom landing + FOLLOW attach once the tail is fully loaded (see
  // runAfterRender). Cleared by any upward wheel.
  let pendingTailIntent = false;
  // The mirror for the other edge: the user targeted the very top while older
  // history was still unloaded. Once event 0 is loaded, pin scrollTop to 0 --
  // without this, a chunked backfill lands wherever the height estimates put
  // the anchor, visibly short of the beginning.
  let pendingTopIntent = false;

  // --- input classification -------------------------------------------------
  let lastInputSource: ScrollInputSource = "wheel";
  // When the user last touched a native input (wheel/keys/pointer). macOS
  // momentum keeps emitting wheel events, so "no input for a while" reliably
  // separates browser adjustments from genuine scrolling.
  // Negative infinity, not 0: performance.now() starts near 0 at page load, so
  // a 0 default would read as "recent input" for the first activity window and
  // misclassify the initial fill's giant clamp as a user scroll.
  let lastNativeInputAtMs = Number.NEGATIVE_INFINITY;
  const pendingEchoTops: number[] = [];
  // Last observed scrollHeight, to recognize the browser's own clamp of
  // scrollTop after content shrank (no user intent; must not be reduced).
  let lastScrollHeightPx = 0;

  // --- fill -----------------------------------------------------------------
  let fillInFlight = false;
  // Bumped by setAgent so a fill still in flight for the previous agent cannot
  // apply its completion (clearing the single-flight guard out from under the
  // new agent's fill, or landing the old agent's jump) after the switch.
  let fillEpoch = 0;
  // A landed fill that changed nothing (a page fully deduped away, a failed
  // fetch, a stale cursor) must not be refired verbatim: the planner would loop
  // the identical request forever. Remember the fruitless action and the data
  // version it ran against; the same plan is skipped until the data moves.
  let noProgressActionKey: string | null = null;
  let noProgressRenderVersion = -1;
  /** Scrollbar target (or restore target) in a virtual region awaiting its window. */
  let pendingJumpIndex: number | null = null;
  /** A landed at-offset fetch whose JUMPED_TO_INDEX dispatch awaits fresh geometry. */
  let pendingJumpLandIndex: number | null = null;

  // --- selection freeze -----------------------------------------------------
  let freezeRange: VisibleRowRange | null = null;

  // Positioning is EVENT-DRIVEN, not continuous: during pure native scrolling
  // the engine writes nothing (nothing competes with the browser). A single
  // compensation write happens only when the content actually changed under
  // the viewport -- this key captures those inputs.
  let lastPositionedKey = "";
  // afterRender work deferred to a microtask (see afterRender); one per task.
  let isAfterRenderQueued = false;

  // --- persistence / restore ------------------------------------------------
  let persistAgentKey: string | null = null;
  let pendingRestore: RestoredScrollState | null = null;
  let persistTimer: ReturnType<typeof setTimeout> | null = null;

  // --- scrollbar interaction ------------------------------------------------
  let lastScrollbarFraction: number | null = null;
  let frozenThumbSizeFraction: number | null = null;
  let lastActivityAtMs = 0;

  const trace: ScrollTrace | null = isTraceEnabled()
    ? createScrollTrace({ capacityEntryCount: TRACE_CAPACITY, now: () => performance.now(), echo: null })
    : null;
  const debugHandles =
    trace !== null
      ? {
          __scrollTrace: {
            dump: () => trace.entries(),
            clear: () => trace.clear(),
          },
          __scrollDebugState: () => ({
            positionKind: positionState.kind,
            scrollbarKind: scrollbarState.kind,
            extent: extent(),
            totalEvents: dataSource.getTotalEvents(),
            capEvents: PHYSICAL_CAP_EVENTS,
            spacerTopPx,
            spacerBottomPx,
            estimatePxPerEvent,
            unmeasuredCount: geometry?.unmeasuredCount ?? null,
            fillInFlight,
          }),
        }
      : null;
  if (debugHandles !== null) {
    // Window globals are last-engine-wins (hidden dockview panels overwrite
    // them); attach() also puts the handles on this engine's own scroll
    // element, which is unambiguous when several transcripts are mounted.
    Object.assign(window as unknown as Record<string, unknown>, debugHandles);
  }

  const offscreenMeasurer = createOffscreenMeasurer({
    getHostEl: () => scrollEl?.parentElement ?? null,
    getListWidthPx: () => lastListWidthPx,
    onHeights: (measured) => {
      let changed = 0;
      for (const [key, heightPx] of measured) {
        // Live measurement wins: offscreen only fills gaps.
        if (!heightByRowKey.has(key)) {
          heightByRowKey.set(key, heightPx);
          changed += 1;
        }
      }
      if (changed > 0) {
        trace?.record("measure-offscreen", { count: changed });
        heightsEpoch += 1;
        m.redraw();
      }
    },
  });

  // --- small helpers --------------------------------------------------------

  function extent(): PhysicalExtent {
    const firstIndex = dataSource.getFirstOffset();
    return { firstIndex, endIndex: firstIndex + cachedWindowEventIds.length };
  }

  function viewportNow(): Viewport {
    const fallbackHeight = scrollEl?.clientHeight ?? 2000;
    return {
      scrollTopPx,
      heightPx: viewportHeightPx > 0 ? viewportHeightPx : fallbackHeight,
      spacerTopPx,
      spacerBottomPx,
    };
  }

  function physicalHeightPx(): number {
    return geometry?.totalHeightPx ?? 0;
  }

  function currentMapping(): ScrollbarMapping {
    return computeLiveMapping({ totalEvents: dataSource.getTotalEvents() ?? 0 }, extent(), physicalHeightPx());
  }

  function activeMapping(): ScrollbarMapping {
    return scrollbarState.kind === "SCROLLBAR" ? scrollbarState.frozen : currentMapping();
  }

  function dispatchPosition(event: ScrollPositionEvent): void {
    // The reducer returns the same reference only when nothing changed (a
    // re-anchor is always a fresh object), so identity is the full no-op test.
    const next = reduceScrollPosition(positionState, event);
    if (next !== positionState) {
      trace?.record("transition", { from: positionState.kind, to: next.kind, event: event.kind });
      positionState = next;
      schedulePersist();
    }
  }

  function markOtherInteraction(): void {
    const next = reduceScrollbarInteraction(scrollbarState, { kind: "OTHER_INTERACTION" });
    if (next !== scrollbarState) {
      scrollbarState = next;
      lastScrollbarFraction = null;
      frozenThumbSizeFraction = null;
    }
  }

  /**
   * The anchor to store for a user position: like anchorFromViewport, but never
   * the window's FIRST row while older history remains unloaded. That boundary
   * row (the partial turn at the window start) keeps its key while absorbing
   * every backfilled page above, so an anchor on it follows the boundary up
   * into newly loaded content. Anchoring to the first interior row pins to
   * content that stays put instead.
   */
  function anchorForUser(): ScrollAnchor | null {
    if (geometry === null) {
      return null;
    }
    const anchor = anchorFromViewport(geometry, viewportNow());
    if (anchor === null) {
      return anchor;
    }
    const hasMoreBefore = extent().firstIndex > 0;
    if (!hasMoreBefore || geometry.rowKeys.length < 2 || anchor.rowKey !== geometry.rowKeys[0]) {
      return anchor;
    }
    const viewportTopContentPx = viewportNow().scrollTopPx - spacerTopPx;
    return { rowKey: geometry.rowKeys[1], offsetPx: geometry.rowTops[1] - viewportTopContentPx };
  }

  function currentAnchorEventIndex(): number | null {
    if (positionState.kind !== "USER_CONTROLLED" || geometry === null) {
      return null;
    }
    const rowIndex = rowIndexOfKey(geometry, positionState.anchor.rowKey);
    if (rowIndex === null || rowIndex >= rowEventIndexes.length) {
      return null;
    }
    return rowEventIndexes[rowIndex];
  }

  /** The anchor's last known event index, resolvable even after its row vanished. */
  function currentAnchorEventIndexFallback(): number | null {
    const direct = currentAnchorEventIndex();
    if (direct !== null) {
      return direct;
    }
    if (positionState.kind !== "USER_CONTROLLED") {
      return null;
    }
    // Row gone: approximate from the viewport's position within the window.
    const spacerIndex = impliedSpacerEventIndex();
    if (spacerIndex !== null) {
      return spacerIndex;
    }
    const { firstIndex, endIndex } = extent();
    if (endIndex <= firstIndex) {
      return null;
    }
    const contentFraction = physicalHeightPx() > 0 ? (scrollTopPx - spacerTopPx) / physicalHeightPx() : 0;
    return firstIndex + Math.round(Math.min(1, Math.max(0, contentFraction)) * (endIndex - firstIndex - 1));
  }

  // --- persistence ----------------------------------------------------------

  function schedulePersist(): void {
    if (persistAgentKey === null || pendingRestore !== null) {
      return;
    }
    if (persistTimer !== null) {
      clearTimeout(persistTimer);
    }
    persistTimer = setTimeout(() => {
      persistTimer = null;
      persistNow();
    }, PERSIST_DEBOUNCE_MS);
  }

  function persistNow(): void {
    if (persistAgentKey === null) {
      return;
    }
    try {
      localStorage.setItem(
        scrollStateStorageKey(persistAgentKey),
        encodePersistedScrollState(positionState, currentAnchorEventIndex()),
      );
    } catch {
      // Quota or privacy mode: persistence is best-effort.
    }
  }

  function loadPersisted(agentKey: string): RestoredScrollState {
    try {
      return decodePersistedScrollState(localStorage.getItem(scrollStateStorageKey(agentKey)));
    } catch {
      return FOLLOW_RESTORED;
    }
  }

  // --- programmatic writes / echo tracking ----------------------------------

  // --- smoothed programmatic writes -----------------------------------------
  // Engine-driven position changes (the streaming follow pin, scrollbar drag
  // steps) land as discrete multi-hundred-px snaps when written directly --
  // each streamed chunk or pointermove teleports the content, which reads as
  // "jumpy". Small-to-medium writes instead glide: a per-frame loop moves a
  // fixed fraction of the remaining distance (echo-tracked writes, so the
  // state machines never see them as input) and any genuine user input
  // cancels the glide instantly. Large writes (track jumps, initial pins)
  // still snap -- gliding across thousands of px would feel like animation.
  let smoothTargetPx: number | null = null;
  let smoothReason = "";
  let smoothRafId: number | null = null;
  const SMOOTH_STEP_FRACTION = 0.4;
  const SMOOTH_MIN_STEP_PX = 24;

  function cancelSmoothScroll(): void {
    if (smoothRafId !== null) {
      cancelAnimationFrame(smoothRafId);
      smoothRafId = null;
    }
    smoothTargetPx = null;
  }

  function smoothStep(): void {
    smoothRafId = null;
    if (smoothTargetPx === null || scrollEl === null) {
      return;
    }
    const element = scrollEl;
    const remainingPx = smoothTargetPx - element.scrollTop;
    if (Math.abs(remainingPx) <= 1) {
      writeScrollTop(element, smoothTargetPx, smoothReason);
      smoothTargetPx = null;
      realignAnchorToGlide();
      m.redraw();
      return;
    }
    const stepPx = Math.sign(remainingPx) * Math.max(SMOOTH_MIN_STEP_PX, Math.abs(remainingPx) * SMOOTH_STEP_FRACTION);
    const nextPx = Math.abs(stepPx) >= Math.abs(remainingPx) ? smoothTargetPx : element.scrollTop + stepPx;
    writeScrollTop(element, nextPx, smoothReason);
    realignAnchorToGlide();
    // Keep the mounted window tracking the gliding viewport (echo-consumed
    // scroll events do not redraw on their own).
    m.redraw();
    smoothRafId = requestAnimationFrame(smoothStep);
  }

  /**
   * Keep a USER_CONTROLLED anchor in lockstep with an engine glide. The glide's
   * writes are echo-tracked, so their scroll events never re-anchor through the
   * state machine; without this the anchor stays where the glide STARTED, and
   * the next content change (a measurement landing, a spacer update) would
   * anchor-hold the viewport back there -- a backward teleport undoing the end
   * of a scrollbar drag -- and persistence would store that stale position.
   */
  function realignAnchorToGlide(): void {
    if (positionState.kind !== "USER_CONTROLLED" || pendingRestore !== null) {
      return;
    }
    const anchor = anchorForUser();
    if (
      anchor !== null &&
      (anchor.rowKey !== positionState.anchor.rowKey || anchor.offsetPx !== positionState.anchor.offsetPx)
    ) {
      trace?.record("glide-realign", { anchor });
      positionState = { kind: "USER_CONTROLLED", anchor };
      schedulePersist();
    }
  }

  /** Start (or retarget) a glide toward targetPx; glides always run against scrollEl. */
  function smoothWriteScrollTop(targetPx: number, reason: string): void {
    smoothTargetPx = targetPx;
    smoothReason = reason;
    if (smoothRafId === null) {
      smoothStep(); // move immediately; smoothStep schedules its own next frame
    }
  }

  function writeScrollTop(element: HTMLElement, targetPx: number, reason: string): void {
    const beforePx = element.scrollTop;
    element.scrollTop = targetPx;
    const afterPx = element.scrollTop; // browser may clamp
    scrollTopPx = afterPx;
    if (Math.abs(afterPx - beforePx) > 0.25) {
      pendingEchoTops.push(afterPx);
    }
    trace?.record("write", { reason, targetPx, afterPx, deltaPx: afterPx - beforePx });
  }

  // --- live row measurement -------------------------------------------------

  function measureMountedRows(): boolean {
    if (scrollEl === null) {
      return false;
    }
    const listEl = scrollEl.querySelector(".message-list");
    if (listEl === null) {
      return false;
    }
    if (listEl !== observedListEl && listResizeObserver !== null) {
      listResizeObserver.disconnect();
      listResizeObserver.observe(listEl);
      observedListEl = listEl;
    }
    const widthPx = (listEl as HTMLElement).offsetWidth;
    if (widthPx > 0) {
      lastListWidthPx = widthPx;
    }
    let changed = false;
    for (const child of Array.from(listEl.children)) {
      const element = child as HTMLElement;
      if (element.id === "") {
        continue; // spacer
      }
      // Measure the row's outer size: border-box height plus its OWN margins.
      // Rows carry CSS margins that height excludes, and geometry built from
      // bare heights drifts from the DOM by the cumulative margins -- thousands
      // of px deep in a transcript. The margins must be the row's own, not the
      // distance to the next sibling: the list is a flex column (margins never
      // collapse), so next-top minus own-top includes the NEXT row's margin-top
      // and flip-flops as the mount boundary sweeps past (a row measured
      // against the spacer loses its neighbor's margin), which oscillated
      // geometry and bounced the anchored viewport. This matches the offscreen
      // measurer's convention exactly.
      const style = getComputedStyle(element);
      const pitchPx =
        element.getBoundingClientRect().height +
        (parseFloat(style.marginTop) || 0) +
        (parseFloat(style.marginBottom) || 0);
      if (pitchPx <= 0) {
        continue;
      }
      const cached = heightByRowKey.get(element.id);
      if (cached === undefined || Math.abs(pitchPx - cached) > MEASURE_HYSTERESIS_PX) {
        heightByRowKey.set(element.id, pitchPx);
        changed = true;
        trace?.record("measure-live", { key: element.id, px: pitchPx, fromPx: cached ?? null });
      }
    }
    return changed;
  }

  /**
   * Whether the FOLLOW pin has nothing left to chase: the agent is not
   * generating, no fill or measurement is outstanding, the tail is fully
   * loaded, and no append has landed since the last pin. While quiescent the
   * pin must not pull the viewport down -- an idle transcript relayout
   * (expanding a block at the tail) must not drag the user -- and the render
   * plan windows around the CURRENT viewport rather than the theoretical
   * bottom, so the two never disagree about where rows should be mounted.
   */
  function isFollowQuiescent(): boolean {
    const totalEventsNow = dataSource.getTotalEvents();
    return (
      !isStreaming() &&
      !fillInFlight &&
      !hasUnfollowedAppend &&
      spacerTopPx <= 0 &&
      spacerBottomPx <= 0 &&
      (geometry === null || geometry.unmeasuredCount === 0) &&
      (totalEventsNow === null || extent().endIndex >= totalEventsNow)
    );
  }

  // --- geometry -------------------------------------------------------------

  function refreshGeometry(): void {
    const renderVersion = dataSource.getRenderVersion();
    const rows = dataSource.getRows();
    const rowsChanged = renderVersion !== cachedRenderVersion || rows !== geometryRows;
    if (!rowsChanged && geometryHeightsEpoch === heightsEpoch && geometry !== null) {
      return;
    }
    if (rowsChanged) {
      cachedWindowEventIds = dataSource.getWindowEventIds();
      // New events past the previous end are an append the FOLLOW pin still
      // owes a scroll for -- even if the agent already reads idle by the time
      // the events land (a completed message often arrives as a late append).
      const endIndexNow = dataSource.getFirstOffset() + cachedWindowEventIds.length;
      if (lastSeenEndIndex !== -1 && endIndexNow > lastSeenEndIndex) {
        hasUnfollowedAppend = true;
      }
      lastSeenEndIndex = endIndexNow;
      rowEventIndexes = buildRowEventIndexes(
        rows.map((row) => row.anchorEventId),
        cachedWindowEventIds,
        dataSource.getFirstOffset(),
      );
      // Bound the height cache as rows are evicted.
      if (heightByRowKey.size > rows.length + 512) {
        const liveKeys = new Set(rows.map((row) => row.key));
        for (const key of heightByRowKey.keys()) {
          if (!liveKeys.has(key)) {
            heightByRowKey.delete(key);
          }
        }
      }
      cachedRenderVersion = renderVersion;
      geometryRows = rows;
    }
    geometry = buildPhysicalGeometry(
      geometryRows.map((row) => ({
        key: row.key,
        measuredPx: heightByRowKey.get(row.key) ?? null,
        estimatePx: row.estimate,
      })),
    );
    geometryHeightsEpoch = heightsEpoch;
    if (geometry.unmeasuredCount > 0) {
      offscreenMeasurer.requestMeasure(geometryRows.filter((row) => !heightByRowKey.has(row.key)));
    }
  }

  // --- fill loop ------------------------------------------------------------

  function impliedSpacerEventIndex(): number | null {
    const { firstIndex, endIndex } = extent();
    const viewport = viewportNow();
    if (spacerTopPx > 0 && viewport.scrollTopPx < spacerTopPx) {
      return Math.round((viewport.scrollTopPx / spacerTopPx) * firstIndex);
    }
    const loadedBottomPx = spacerTopPx + physicalHeightPx();
    const viewportBottomPx = viewport.scrollTopPx + viewport.heightPx;
    if (spacerBottomPx > 0 && viewportBottomPx > loadedBottomPx) {
      const total = dataSource.getTotalEvents() ?? endIndex;
      const intoSpacerPx = viewportBottomPx - loadedBottomPx;
      return endIndex + Math.round((intoSpacerPx / spacerBottomPx) * (total - endIndex));
    }
    return null;
  }

  function fillFocus(): FillFocus {
    if (pendingJumpIndex !== null) {
      return { kind: "index", index: pendingJumpIndex };
    }
    if (positionState.kind === "FOLLOW") {
      return { kind: "tail" };
    }
    const spacerIndex = impliedSpacerEventIndex();
    if (spacerIndex !== null) {
      return { kind: "index", index: spacerIndex };
    }
    // The fallback matters: right after a jump's window replace the anchor row
    // is briefly gone, and a plain lookup would read that as "focus the tail" --
    // sending the planner off to replace the fresh window with a tail window,
    // which discards the jump and churns fetch/evict cycles indefinitely.
    const anchorIndex = currentAnchorEventIndexFallback();
    return anchorIndex !== null ? { kind: "index", index: anchorIndex } : { kind: "tail" };
  }

  function planFill(): void {
    // pendingJumpLandIndex: a jump's window has landed but the viewport has not
    // been re-anchored onto it yet (that happens in computeRenderPlan). Planning
    // against the stale anchor now would immediately replace the window again.
    if (fillInFlight || pendingJumpLandIndex !== null || !isVisible()) {
      return;
    }
    const totalEvents = dataSource.getTotalEvents();
    if (totalEvents === null) {
      return; // initial snapshot load (with SSE buffering) is the view's own flow
    }
    const action = planNextFill({
      physical: extent(),
      totalEvents,
      focus: fillFocus(),
      capEvents: PHYSICAL_CAP_EVENTS,
      chunkLimit: FILL_CHUNK_LIMIT,
      initialTailLimit: INITIAL_TAIL_LIMIT,
      jumpWindowLimit: JUMP_WINDOW_LIMIT,
    });
    if (action.kind === "idle") {
      return;
    }
    if (action.kind === "evict" && isSelectionActiveWithin(selectionStateWithin(scrollEl))) {
      return; // eviction deletes events under a live selection; wait it out
    }
    const actionKey = JSON.stringify(action);
    if (actionKey === noProgressActionKey && dataSource.getRenderVersion() === noProgressRenderVersion) {
      return; // this exact action already landed without changing anything
    }
    fillInFlight = true;
    trace?.record("fill", { action });
    const jumpIndexAtDispatch = pendingJumpIndex;
    const epochAtDispatch = fillEpoch;
    const renderVersionAtDispatch = dataSource.getRenderVersion();
    dataSource
      .executeFill(action)
      .catch((error: unknown) => {
        // Expected network failures are caught (and logged) inside the fetch
        // helpers; anything landing here is unexpected and must not vanish.
        console.warn("Transcript fill action failed", action, error);
      })
      .then(() => {
        if (fillEpoch !== epochAtDispatch) {
          return; // setAgent reset everything; this completion is the old agent's
        }
        fillInFlight = false;
        if (dataSource.getRenderVersion() === renderVersionAtDispatch) {
          noProgressActionKey = actionKey;
          noProgressRenderVersion = renderVersionAtDispatch;
        } else {
          noProgressActionKey = null;
        }
        if (action.kind === "fetch-at-offset" && jumpIndexAtDispatch !== null) {
          // The jump's window landed; anchor to the target once geometry rebuilds.
          pendingJumpLandIndex = jumpIndexAtDispatch;
          if (pendingJumpIndex === jumpIndexAtDispatch) {
            pendingJumpIndex = null;
          }
        }
        m.redraw();
      });
  }

  // --- restore --------------------------------------------------------------

  function tryFinishRestore(): void {
    if (pendingRestore === null || geometry === null) {
      return;
    }
    if (pendingRestore.state.kind === "FOLLOW") {
      positionState = FOLLOW_STATE;
      pendingRestore = null;
      return;
    }
    const anchorIndex = pendingRestore.anchorEventIndex;
    const { firstIndex, endIndex } = extent();
    const totalEvents = dataSource.getTotalEvents();
    const isTargetLoaded =
      anchorIndex === null ||
      (anchorIndex >= firstIndex && anchorIndex < endIndex) ||
      (totalEvents !== null && anchorIndex >= totalEvents);
    if (!isTargetLoaded) {
      // Keep steering the fill toward the persisted location.
      pendingJumpIndex = anchorIndex;
      return;
    }
    const validated = validateRestoredAnchor(pendingRestore, (key) => rowIndexOfKey(geometry!, key) !== null);
    trace?.record("restore", { requested: pendingRestore.state, outcome: validated.kind });
    positionState = validated;
    pendingRestore = null;
    if (pendingJumpIndex === anchorIndex) {
      pendingJumpIndex = null;
    }
    // The restore's own fill also lands as a jump; the restored anchor (with its
    // preserved offset) must not be overwritten by that landing's offset-0 anchor.
    pendingJumpLandIndex = null;
    schedulePersist();
  }

  // --- input listeners ------------------------------------------------------

  function onScrollEvent(event: Event): void {
    const element = event.target as HTMLElement;
    const topPx = element.scrollTop;
    // The browser coalesces scroll events, so one event can stand for several
    // queued programmatic writes: match against ANY pending echo and consume
    // everything up to it.
    const echoIndex = pendingEchoTops.findIndex((echoPx) => Math.abs(topPx - echoPx) <= ECHO_TOLERANCE_PX);
    if (echoIndex !== -1) {
      pendingEchoTops.splice(0, echoIndex + 1);
      scrollTopPx = topPx;
      lastScrollHeightPx = element.scrollHeight;
      trace?.record("scroll-echo", { topPx });
      return;
    }
    // The browser clamping scrollTop after content shrank (a spacer eased down,
    // rows collapsed) carries no user intent: sync bookkeeping, don't reduce.
    const scrollHeightNow = element.scrollHeight;
    const isShrinkClamp =
      scrollHeightNow < lastScrollHeightPx && Math.abs(topPx - (scrollHeightNow - element.clientHeight)) <= 1;
    lastScrollHeightPx = scrollHeightNow;
    if (isShrinkClamp) {
      pendingEchoTops.length = 0;
      scrollTopPx = topPx;
      trace?.record("scroll-clamp", { topPx });
      return;
    }
    // A notification at the position we already hold is movement-free (late
    // clamp echoes arrive like this): nothing to express, never reduce it.
    // Exact comparison: a real sub-pixel user movement must still reduce.
    if (Math.abs(topPx - scrollTopPx) <= 0.01) {
      trace?.record("scroll-noop", { topPx });
      return;
    }
    pendingEchoTops.length = 0; // a genuine scroll invalidates stale echoes
    cancelSmoothScroll(); // real input supersedes any engine glide in flight
    const didScrollUp = topPx < scrollTopPx;
    scrollTopPx = topPx;
    lastActivityAtMs = performance.now();
    if (geometry === null) {
      return;
    }
    const anchor = anchorForUser();
    if (anchor === null) {
      return;
    }
    // ANY upward movement is intent to leave the tail, even within the bottom
    // band: a trackpad gesture's first events move only a few px, and deciding
    // by position alone would re-pin the user to the bottom until one event
    // cleared the band -- the "stuck at the bottom" feel.
    const bottomGapPx = element.scrollHeight - topPx - element.clientHeight;
    const totalEvents = dataSource.getTotalEvents();
    const atTail =
      !didScrollUp &&
      bottomGapPx < BOTTOM_THRESHOLD_PX &&
      spacerBottomPx <= 0 &&
      (totalEvents === null || extent().endIndex >= totalEvents);
    // A downward scroll ending in the bottom band expresses "go to the tail"
    // even when the fill still lags the server total (atTail false only for
    // data reasons). Remember the intent; runAfterRender completes the FOLLOW
    // attach once the tail is fully loaded -- without this, a fling to the
    // bottom during streaming strands the user detached at gap 0, where the
    // clamped scrollTop emits no further scroll events to re-evaluate.
    pendingTailIntent = !didScrollUp && bottomGapPx < BOTTOM_THRESHOLD_PX;
    // Mirror for the top edge: an upward scroll ending at (clamped) scrollTop 0
    // while older history remains unloaded means "go to the beginning".
    pendingTopIntent = didScrollUp && topPx < BOTTOM_THRESHOLD_PX;
    trace?.record("scroll", {
      topPx,
      source: lastInputSource,
      atTail,
      anchor,
      gapPx: bottomGapPx,
      scrollHeightPx: element.scrollHeight,
      clientHeightPx: element.clientHeight,
      bookkeptTopPx: scrollTopPx,
    });
    dispatchPosition({ kind: "USER_SCROLLED", source: lastInputSource, anchor, atTail });
    m.redraw();
    planFill();
  }

  function onWheel(event: WheelEvent): void {
    cancelSmoothScroll();
    lastNativeInputAtMs = performance.now();
    lastInputSource = "wheel";
    lastActivityAtMs = performance.now();
    markOtherInteraction();
    // ANY upward wheel intent detaches from the tail immediately -- before the
    // native scroll even happens, so no pin or echo bookkeeping can eat it.
    // The bottom band is only for RE-attaching on the way down.
    if (event.deltaY < 0) {
      pendingTailIntent = false;
      // An upward wheel already clamped at the top produces no scroll event;
      // record the top intent here so backfill still lands on the beginning.
      if (scrollEl !== null && scrollEl.scrollTop < BOTTOM_THRESHOLD_PX) {
        pendingTopIntent = true;
      }
      if (positionState.kind === "FOLLOW" && geometry !== null) {
        const anchor = anchorForUser();
        if (anchor !== null) {
          trace?.record("wheel-detach", { deltaY: event.deltaY });
          dispatchPosition({ kind: "USER_SCROLLED", source: "wheel", anchor, atTail: false });
          m.redraw();
        }
      }
    } else if (event.deltaY > 0 && scrollEl !== null) {
      pendingTopIntent = false;
      // A downward wheel already clamped at the bottom produces no scroll
      // event at all; record the tail intent here so it still re-attaches.
      const gapPx = scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight;
      if (gapPx < BOTTOM_THRESHOLD_PX) {
        pendingTailIntent = true;
      }
    }
  }

  function onKeyDown(): void {
    cancelSmoothScroll();
    lastNativeInputAtMs = performance.now();
    lastInputSource = "keyboard";
    markOtherInteraction();
  }

  function onPointerDown(): void {
    // A drag over the transcript is likely a selection: defer the FOLLOW pin
    // while the button is held, and treat edge autoscroll as its own source.
    cancelSmoothScroll();
    lastNativeInputAtMs = performance.now();
    lastInputSource = "selection-autoscroll";
    isPointerDown = true;
    markOtherInteraction();
  }

  // --- attach / detach ------------------------------------------------------

  function attach(element: HTMLElement): void {
    if (scrollEl === element) {
      return;
    }
    detachListeners();
    scrollEl = element;
    if (debugHandles !== null) {
      Object.assign(element as unknown as Record<string, unknown>, debugHandles);
    }
    element.addEventListener("scroll", onScrollEvent, { passive: true });
    element.addEventListener("wheel", onWheel as EventListener, { passive: true });
    element.addEventListener("keydown", onKeyDown);
    element.addEventListener("pointerdown", onPointerDown);
    pointerReleaseListener = () => {
      if (isPointerDown) {
        isPointerDown = false;
        m.redraw();
      }
    };
    window.addEventListener("pointerup", pointerReleaseListener);
    window.addEventListener("pointercancel", pointerReleaseListener);
    if (isVisible()) {
      viewportHeightPx = element.clientHeight;
    }
    lastScrollHeightPx = element.scrollHeight;
    listResizeObserver = new ResizeObserver(() => {
      m.redraw();
    });
    resizeObserver = new ResizeObserver(() => {
      if (scrollEl === null || !isVisible()) {
        return;
      }
      if (scrollEl.clientHeight !== viewportHeightPx) {
        viewportHeightPx = scrollEl.clientHeight;
        m.redraw();
      }
      const listEl = scrollEl.querySelector(".message-list");
      const widthPx = listEl instanceof HTMLElement ? listEl.offsetWidth : null;
      if (widthPx !== null && widthPx > 0 && lastListWidthPx !== null && Math.abs(widthPx - lastListWidthPx) > 1) {
        // Width changed: every measured height is invalid. Full invalidate;
        // visible rows re-measure on the next frame, the offscreen pass re-runs,
        // and anchor positioning holds the reading position through the shuffle.
        trace?.record("width-invalidate", { fromPx: lastListWidthPx, toPx: widthPx });
        lastListWidthPx = widthPx;
        heightByRowKey.clear();
        offscreenMeasurer.cancel();
        heightsEpoch += 1;
        m.redraw();
      }
    });
    resizeObserver.observe(element);
  }

  function detachListeners(): void {
    if (scrollEl !== null) {
      scrollEl.removeEventListener("scroll", onScrollEvent);
      scrollEl.removeEventListener("wheel", onWheel);
      scrollEl.removeEventListener("keydown", onKeyDown);
      scrollEl.removeEventListener("pointerdown", onPointerDown);
    }
    if (pointerReleaseListener !== null) {
      window.removeEventListener("pointerup", pointerReleaseListener);
      window.removeEventListener("pointercancel", pointerReleaseListener);
      pointerReleaseListener = null;
    }
    resizeObserver?.disconnect();
    resizeObserver = null;
    listResizeObserver?.disconnect();
    listResizeObserver = null;
    observedListEl = null;
    scrollEl = null;
  }

  function runAfterRender(element: HTMLElement): void {
    if (!isVisible()) {
      return;
    }
    // Measure the mounted rows NOW, before positioning: a tall row mounting
    // into the overscan renders at its real height this same frame, and
    // positioning against last frame's estimates would paint a one-frame
    // shift proportional to the estimate error (visible on long messages).
    if (measureMountedRows()) {
      heightsEpoch += 1;
      refreshGeometry();
      m.redraw(); // pads/ranges were computed pre-measure; re-plan next frame
    }

    // A native scroll whose event hasn't been handled yet (wheel/keys
    // mid-flight) shows up as a gap between the DOM position and our
    // bookkeeping. It must be neither swallowed (writing the stale target
    // would yank the viewport back) nor left to meet moved geometry (this
    // frame may have resized spacers/rows above, so the queued event's
    // position no longer means what the user saw). Fold it in instead:
    // compensate AND preserve the user's delta in one write, consume the
    // stale event as an echo, and run the state machine on the result now.
    const pendingUserDeltaPx = element.scrollTop - scrollTopPx;
    // This frame's DOM update may have shrunk the content, making the browser
    // clamp scrollTop before we ran: that gap is not user input. The clamp can
    // land short of the FINAL maximum (a mid-patch forced layout clamps against
    // intermediate geometry), so besides the at-max signature, any sizeable
    // upward gap with no recent native input is an adjustment. Consume its
    // event as an echo and reposition normally.
    const maxScrollPx = Math.max(0, element.scrollHeight - element.clientHeight);
    const isRecentNativeInput = performance.now() - lastNativeInputAtMs < 150;
    const isPendingClamp =
      pendingUserDeltaPx < -0.01 && (Math.abs(element.scrollTop - maxScrollPx) <= 1 || !isRecentNativeInput);
    // NO minimum size: a trackpad gesture starts as a stream of sub-pixel
    // deltas, and while streaming redraws run every frame, any threshold here
    // reverts each tiny delta before the next arrives -- pinning the user in
    // place. Bookkeeping is exact, so exact comparison is safe.
    const hasPendingUserScroll = Math.abs(pendingUserDeltaPx) > 0.01 && !isPendingClamp;
    if (isPendingClamp) {
      pendingEchoTops.push(element.scrollTop);
      trace?.record("clamp-absorbed", { deltaPx: pendingUserDeltaPx });
    }

    // Positioning: the single writer of scrollTop, and only when the content
    // changed (heights, rows, spacers) or the state machine moved -- never in
    // response to plain native scrolling, which stays entirely the browser's.
    const positionKey =
      positionState.kind +
      "|" +
      (positionState.kind === "USER_CONTROLLED"
        ? positionState.anchor.rowKey + "@" + positionState.anchor.offsetPx
        : "") +
      "|" +
      heightsEpoch +
      "|" +
      cachedRenderVersion +
      "|" +
      spacerTopPx +
      "|" +
      spacerBottomPx +
      "|" +
      element.scrollHeight +
      "|" +
      element.clientHeight;
    if (positionKey === lastPositionedKey) {
      planFill();
      return;
    }
    lastPositionedKey = positionKey;

    if (positionState.kind === "FOLLOW") {
      if (isPointerDown || (pendingUserDeltaPx < -0.01 && !isPendingClamp)) {
        trace?.record("follow-yield", { pendingUserDeltaPx, isPointerDown });
      }
      // FOLLOW means AT the bottom, always. The quiescence gate exists only
      // so a relayout caused by the user's own in-transcript interaction (an
      // expand click) does not fight them mid-gesture; a gap that opens with
      // no recent native input -- a late image load, a font swap, a delayed
      // re-measure of freshly mounted tail rows -- is re-pinned even when
      // everything else is quiescent, otherwise a drag-to-bottom strands the
      // viewport above the tail with nothing left to close the gap.
      const isRecentUserInteraction = performance.now() - lastNativeInputAtMs < 500;
      if (
        !isPointerDown &&
        (!isFollowQuiescent() || !isRecentUserInteraction) &&
        (pendingUserDeltaPx >= -0.01 || isPendingClamp)
      ) {
        const targetPx = element.scrollHeight - element.clientHeight;
        if (hasPendingUserScroll) {
          pendingEchoTops.push(element.scrollTop);
        }
        const pinDeltaPx = targetPx - element.scrollTop;
        // Glide only for steady streaming growth. During fill/measurement
        // churn content grows hundreds of px per frame and a glide would lag
        // it into a visible sustained gap; and a large catch-up would read as
        // animation. Both snap.
        const isChurning =
          fillInFlight || spacerTopPx > 0 || spacerBottomPx > 0 || (geometry !== null && geometry.unmeasuredCount > 0);
        if (Math.abs(pinDeltaPx) > 0.5) {
          if (!isChurning && Math.abs(pinDeltaPx) <= element.clientHeight * 1.5) {
            smoothWriteScrollTop(targetPx, "follow-pin");
          } else {
            cancelSmoothScroll();
            writeScrollTop(element, targetPx, "follow-pin");
          }
        } else {
          scrollTopPx = element.scrollTop;
        }
        hasUnfollowedAppend = false;
      }
    } else if (geometry !== null) {
      let targetPx = scrollTopForAnchor(geometry, positionState.anchor, spacerTopPx);
      if (targetPx === null && pendingRestore !== null) {
        // A restored anchor whose window has not loaded yet is not lost --
        // repairing now would replace it with a nonsense near-tail anchor.
        // Hold position; the restore flow validates once its window lands.
        planFill();
        return;
      }
      if (targetPx === null) {
        // The anchor row vanished (turn regrouping, eviction): repair to the
        // row now covering the same transcript position instead of jumping.
        const anchorIndex = currentAnchorEventIndexFallback();
        const rowIndex = anchorIndex === null ? -1 : rowIndexForEventIndex(rowEventIndexes, anchorIndex);
        if (rowIndex >= 0) {
          const repaired: ScrollAnchor = { rowKey: geometryRows[rowIndex].key, offsetPx: 0 };
          trace?.record("anchor-repair", { toRowKey: repaired.rowKey, atEventIndex: anchorIndex });
          positionState = { kind: "USER_CONTROLLED", anchor: repaired };
          targetPx = scrollTopForAnchor(geometry, repaired, spacerTopPx);
        }
      }
      if (targetPx !== null) {
        const heldPx = targetPx + (hasPendingUserScroll ? pendingUserDeltaPx : 0);
        if (hasPendingUserScroll) {
          pendingEchoTops.push(element.scrollTop);
        }
        if (Math.abs(element.scrollTop - heldPx) > 0.5) {
          cancelSmoothScroll();
          writeScrollTop(element, heldPx, "anchor-hold");
        } else {
          scrollTopPx = element.scrollTop;
        }
        if (hasPendingUserScroll) {
          // The user's in-flight scroll was preserved through the write; feed
          // it to the state machine now (its own event was consumed above).
          const foldedAnchor = anchorForUser();
          if (foldedAnchor !== null) {
            const bottomGapPx = element.scrollHeight - element.scrollTop - element.clientHeight;
            const totalEvents = dataSource.getTotalEvents();
            const atTail =
              pendingUserDeltaPx > 0 &&
              bottomGapPx < BOTTOM_THRESHOLD_PX &&
              spacerBottomPx <= 0 &&
              (totalEvents === null || extent().endIndex >= totalEvents);
            trace?.record("scroll-fold", { deltaPx: pendingUserDeltaPx, anchor: foldedAnchor, atTail });
            dispatchPosition({ kind: "USER_SCROLLED", source: lastInputSource, anchor: foldedAnchor, atTail });
          }
        }
      }
    }

    // Complete a recorded edge intent (see onScrollEvent / scrollbarMoveTo):
    // the user expressed "go to the very bottom/top" while that edge was not
    // fully loaded, so the exact landing could not happen at gesture time --
    // and a clamped scrollTop emits no further scroll events to retry. Once
    // the edge IS loaded, land exactly on it: a jump anchors its target row at
    // the viewport TOP, so without the snap a tall last row leaves the true
    // bottom below the fold, and a chunked backfill toward event 0 lands
    // wherever the height estimates put the anchor rather than at 0.
    if (pendingTailIntent && positionState.kind === "USER_CONTROLLED" && geometry !== null) {
      const totalNow = dataSource.getTotalEvents();
      if (spacerBottomPx <= 0 && (totalNow === null || extent().endIndex >= totalNow)) {
        const targetPx = Math.max(0, element.scrollHeight - element.clientHeight);
        if (Math.abs(element.scrollTop - targetPx) > 0.5) {
          cancelSmoothScroll();
          writeScrollTop(element, targetPx, "tail-intent-pin");
        }
        const tailAnchor = anchorForUser();
        if (tailAnchor !== null) {
          pendingTailIntent = false;
          trace?.record("tail-intent-attach", {
            gapPx: element.scrollHeight - element.scrollTop - element.clientHeight,
          });
          dispatchPosition({ kind: "USER_SCROLLED", source: lastInputSource, anchor: tailAnchor, atTail: true });
          m.redraw();
        }
      }
    }
    if (pendingTopIntent && positionState.kind === "USER_CONTROLLED" && geometry !== null) {
      if (extent().firstIndex <= 0 && spacerTopPx <= 0) {
        if (element.scrollTop > 0.5) {
          cancelSmoothScroll();
          writeScrollTop(element, 0, "top-intent-pin");
        }
        const topAnchor = anchorForUser();
        if (topAnchor !== null) {
          pendingTopIntent = false;
          trace?.record("top-intent-pin", { scrollTopPx: element.scrollTop });
          dispatchPosition({ kind: "USER_SCROLLED", source: lastInputSource, anchor: topAnchor, atTail: false });
          m.redraw();
        }
      }
    }

    planFill();
  }

  // --- public API -----------------------------------------------------------

  return {
    computeRenderPlan(): TranscriptRenderPlan {
      refreshGeometry();
      tryFinishRestore();

      // A pending scrollbar target lands as soon as its event is inside the
      // loaded window, whichever way the fill got there -- an at-offset
      // replace or chunked before/after growth. Without this the near-gap
      // path never re-anchors onto the target: the landing is left to
      // spacer-estimate noise (a 3x estimate error lands a 95% release at
      // ~70% of the transcript), and pendingJumpIndex would keep steering
      // the fill at the stale target forever.
      if (pendingJumpIndex !== null && pendingRestore === null && geometry !== null) {
        const { firstIndex, endIndex } = extent();
        if (pendingJumpIndex >= firstIndex && pendingJumpIndex < endIndex) {
          pendingJumpLandIndex = pendingJumpIndex;
          pendingJumpIndex = null;
        }
      }

      // A landed jump anchors to its target row now that geometry covers it.
      // Cleared even when the landing produced no rows (an empty page): leaving
      // it set would hold planFill's landing gate closed forever.
      if (pendingJumpLandIndex !== null && geometry !== null) {
        const rowIndex = geometryRows.length > 0 ? rowIndexForEventIndex(rowEventIndexes, pendingJumpLandIndex) : -1;
        if (rowIndex >= 0) {
          // Same boundary rule as anchorForUser: never anchor the window's first
          // row while older history remains (it absorbs prepends under a stable
          // key); pin the first interior row so the target stays put as fills land.
          const useInteriorRow = rowIndex === 0 && extent().firstIndex > 0 && geometryRows.length >= 2;
          const anchorRowIndex = useInteriorRow ? 1 : rowIndex;
          const offsetPx = geometry.rowTops[anchorRowIndex] - geometry.rowTops[rowIndex];
          const anchor: ScrollAnchor = { rowKey: geometryRows[anchorRowIndex].key, offsetPx };
          dispatchPosition({ kind: "JUMPED_TO_INDEX", anchor });
        }
        pendingJumpLandIndex = null;
      }

      // Spacer sizing. Anchor/bottom positioning in afterRender absorbs any
      // change, so nothing visibly moves. The SCROLLBAR state freezes the
      // track mapping and the px/event ESTIMATE (stable scrubbing math) --
      // but NOT the spacer sizes: a drag that jumps into unloaded history
      // re-centers the physical window, and stale spacers would leave the
      // viewport over an unpadded void (blank, and outside any spacer, so
      // the loading overlay never shows). Sizes always track the extent.
      const totalEvents = dataSource.getTotalEvents() ?? 0;
      const { firstIndex, endIndex } = extent();
      if (scrollbarState.kind === "ELSEWHERE") {
        const update = computeSpacerUpdate({
          previousEstimatePxPerEvent: estimatePxPerEvent,
          observedPxPerEvent: computeObservedPxPerEvent(physicalHeightPx(), endIndex - firstIndex),
          smoothingAlpha: SPACER_SMOOTHING_ALPHA,
          olderUnloadedCount: firstIndex,
          newerUnloadedCount: Math.max(0, totalEvents - endIndex),
          previousSpacerTopPx: spacerTopPx,
        });
        if (update.scrollTopDeltaPx !== 0 || update.spacerBottomPx !== spacerBottomPx) {
          trace?.record("spacer", {
            estimate: update.estimatePxPerEvent,
            topPx: update.spacerTopPx,
            bottomPx: update.spacerBottomPx,
            deltaPx: update.scrollTopDeltaPx,
          });
        }
        estimatePxPerEvent = update.estimatePxPerEvent;
        spacerTopPx = update.spacerTopPx;
        spacerBottomPx = update.spacerBottomPx;
      } else {
        const frozenTopPx = Math.round(Math.max(0, firstIndex) * estimatePxPerEvent);
        const frozenBottomPx = Math.round(Math.max(0, totalEvents - endIndex) * estimatePxPerEvent);
        if (frozenTopPx !== spacerTopPx || frozenBottomPx !== spacerBottomPx) {
          trace?.record("spacer", {
            estimate: estimatePxPerEvent,
            topPx: frozenTopPx,
            bottomPx: frozenBottomPx,
            deltaPx: frozenTopPx - spacerTopPx,
          });
        }
        spacerTopPx = frozenTopPx;
        spacerBottomPx = frozenBottomPx;
      }

      if (geometry === null || geometryRows.length === 0) {
        return { topPadPx: spacerTopPx, bottomPadPx: spacerBottomPx, startIndex: 0, endIndex: 0 };
      }

      // Window the rows around where the viewport will be after positioning.
      // In quiescent FOLLOW the pin does not move the viewport, so the window
      // must track the CURRENT position -- windowing the theoretical bottom
      // there mounts rows the viewport is not over, painting blank space.
      const viewport = viewportNow();
      let windowScrollTopPx = viewport.scrollTopPx;
      if (positionState.kind === "FOLLOW" && !isFollowQuiescent()) {
        windowScrollTopPx = Math.max(0, spacerTopPx + physicalHeightPx() + spacerBottomPx - viewport.heightPx);
      } else if (positionState.kind !== "FOLLOW") {
        const anchoredTopPx = scrollTopForAnchor(geometry, positionState.anchor, spacerTopPx);
        if (anchoredTopPx !== null) {
          windowScrollTopPx = anchoredTopPx;
        }
      }
      let range = computeVisibleRowRange(geometry, { ...viewport, scrollTopPx: windowScrollTopPx }, OVERSCAN_PX);

      // Selection freeze: while a selection is live in this transcript, mounted
      // rows never unmount -- the window only grows. One boolean gate instead of
      // disjoint pinned runs; it self-heals when the selection clears.
      if (isSelectionActiveWithin(selectionStateWithin(scrollEl))) {
        freezeRange =
          freezeRange === null
            ? range
            : {
                startIndex: Math.min(freezeRange.startIndex, range.startIndex),
                endIndex: Math.max(freezeRange.endIndex, range.endIndex),
              };
        range = freezeRange;
      } else {
        freezeRange = null;
      }

      // Pads are exact height sums, so mounted rows always sit at their true
      // offsets within the scroll space.
      const rowCount = geometryRows.length;
      // A retained freeze range can outlive the rows it froze: a jump replaces
      // the window (or an eviction shrinks it) while a selection is live, and
      // the old indexes then run past the new rows array. Rendering such a plan
      // makes rows[i].render() throw on undefined, which kills every subsequent
      // redraw -- the panel freezes on whatever painted last. Clamp to what
      // actually exists.
      if (range.endIndex > rowCount || range.startIndex > rowCount) {
        range = { startIndex: Math.min(range.startIndex, rowCount), endIndex: Math.min(range.endIndex, rowCount) };
        if (freezeRange !== null) {
          freezeRange = range;
        }
      }
      const topPadPx =
        spacerTopPx + (range.startIndex < rowCount ? geometry.rowTops[range.startIndex] : geometry.totalHeightPx);
      const windowEndTopPx = range.endIndex < rowCount ? geometry.rowTops[range.endIndex] : geometry.totalHeightPx;
      const bottomPadPx = spacerBottomPx + (geometry.totalHeightPx - windowEndTopPx);
      return { topPadPx, bottomPadPx, startIndex: range.startIndex, endIndex: range.endIndex };
    },

    afterRender(element: HTMLElement): void {
      attach(element);
      // Defer measurement + positioning to a microtask: mithril runs lifecycle
      // hooks parent-first, so this hook fires BEFORE a newly mounted row's own
      // oncreate has filled its content (MarkdownContent injects innerHTML in
      // oncreate). Measuring synchronously here reads such rows as empty,
      // poisons the height table for a frame, and the anchor compensation
      // computed from it visibly bounces the viewport. A microtask runs after
      // every child hook in the same task, still before the browser paints.
      if (isAfterRenderQueued) {
        return;
      }
      isAfterRenderQueued = true;
      queueMicrotask(() => {
        isAfterRenderQueued = false;
        if (scrollEl !== null) {
          runAfterRender(scrollEl);
        }
      });
    },

    detach(): void {
      cancelSmoothScroll();
      detachListeners();
      offscreenMeasurer.cancel();
      if (persistTimer !== null) {
        clearTimeout(persistTimer);
        persistTimer = null;
        persistNow();
      }
    },

    setAgent(agentKey: string | null): void {
      if (persistTimer !== null) {
        clearTimeout(persistTimer);
        persistTimer = null;
        persistNow();
      }
      persistAgentKey = agentKey;
      positionState = FOLLOW_STATE;
      scrollbarState = ELSEWHERE_STATE;
      heightByRowKey.clear();
      geometry = null;
      geometryRows = [];
      rowEventIndexes = [];
      cachedWindowEventIds = [];
      cachedRenderVersion = -1;
      heightsEpoch += 1;
      estimatePxPerEvent = DEFAULT_SPACER_PX_PER_EVENT;
      spacerTopPx = 0;
      spacerBottomPx = 0;
      scrollTopPx = 0;
      pendingEchoTops.length = 0;
      fillInFlight = false;
      fillEpoch += 1;
      noProgressActionKey = null;
      noProgressRenderVersion = -1;
      pendingJumpIndex = null;
      pendingJumpLandIndex = null;
      pendingTailIntent = false;
      pendingTopIntent = false;
      cancelSmoothScroll();
      lastSeenEndIndex = -1;
      hasUnfollowedAppend = false;
      freezeRange = null;
      lastPositionedKey = "";
      lastScrollbarFraction = null;
      frozenThumbSizeFraction = null;
      offscreenMeasurer.cancel();
      pendingRestore = agentKey !== null ? loadPersisted(agentKey) : null;
      if (pendingRestore !== null && pendingRestore.state.kind === "USER_CONTROLLED") {
        // Steer the first fill toward the persisted location; validated once loaded.
        positionState = pendingRestore.state;
        pendingJumpIndex = pendingRestore.anchorEventIndex;
      } else {
        pendingRestore = null;
      }
    },

    noteMessageSent(): void {
      markOtherInteraction();
      pendingJumpIndex = null;
      pendingRestore = null;
      dispatchPosition({ kind: "MESSAGE_SENT" });
      m.redraw();
    },

    isViewportInSpacer(): boolean {
      const viewport = viewportNow();
      if (spacerTopPx > 0 && viewport.scrollTopPx < spacerTopPx) {
        return true;
      }
      const loadedBottomPx = spacerTopPx + physicalHeightPx();
      return spacerBottomPx > 0 && viewport.scrollTopPx + viewport.heightPx > loadedBottomPx;
    },

    scrollbarEngage(): void {
      lastActivityAtMs = performance.now();
      lastInputSource = "scrollbar";
      const mappingAtEngage = activeMapping();
      const next = reduceScrollbarInteraction(scrollbarState, { kind: "SCROLLBAR_ENGAGED", mappingAtEngage });
      if (next !== scrollbarState) {
        scrollbarState = next;
        frozenThumbSizeFraction = computeThumb(mappingAtEngage, viewportNow(), physicalHeightPx()).sizeFraction;
        trace?.record("scrollbar-engage", { mapping: mappingAtEngage });
      }
    },

    scrollbarMoveTo(fraction: number): void {
      if (scrollEl === null) {
        return;
      }
      lastActivityAtMs = performance.now();
      lastInputSource = "scrollbar";
      lastScrollbarFraction = Math.min(1, Math.max(0, fraction));
      const mapping = activeMapping();
      let target = resolveTrackFraction(mapping, lastScrollbarFraction);
      if (target.kind === "physical-fraction") {
        // The frozen mapping's physical band is trustworthy in PIXEL space only
        // while it still describes the loaded window. Once fills/evictions land
        // mid-drag the band's pixels cover different content, and a px-space
        // resolution would show unrelated old messages. Fall back to index
        // space: the thumb keeps pointing at the same transcript position, and
        // an unloaded target goes through the jump path (loading overlay) below.
        const frozenExtent = mappingPhysicalExtent(mapping);
        const live = extent();
        const isMappingCurrent =
          frozenExtent !== null &&
          frozenExtent.firstIndex === live.firstIndex &&
          frozenExtent.endIndex === live.endIndex;
        if (!isMappingCurrent) {
          target = { kind: "virtual-index", index: resolveTrackFractionToIndex(mapping, lastScrollbarFraction) };
        }
      }
      // The scrollbar clamps the pointer fraction to [0, 1], so exactly 1 (or
      // 0) means the pointer genuinely reached or passed the point where the
      // thumb bottoms (tops) out -- the one unambiguous "take me to the very
      // end/beginning" signal. Everything short of that is an honest linear
      // position; no thresholds or warps second-guess the pointer.
      const isTrackEndIntent = lastScrollbarFraction >= 1 - 1e-6;
      const isTrackStartIntent = lastScrollbarFraction <= 1e-6;
      trace?.record("scrollbar-move", { fraction: lastScrollbarFraction, target });
      if (target.kind === "physical-fraction") {
        // Scale over the region's scrollable span. With a bottom spacer the
        // physical band ends where the spacer begins; without one it runs to the
        // element's true bottom (list padding included), so fraction 1 lands
        // exactly at the tail.
        const maxScrollPx = Math.max(0, scrollEl.scrollHeight - scrollEl.clientHeight);
        const bandEndPx = spacerBottomPx > 0 ? spacerTopPx + physicalHeightPx() : maxScrollPx;
        const spanPx = Math.max(0, bandEndPx - spacerTopPx);
        const targetTopPx = spacerTopPx + target.fraction * spanPx;
        // On a huge transcript each pointermove maps to hundreds or thousands
        // of px; written directly, every move is a visible teleport. Glide
        // between drag steps; a genuine far jump (track click) still snaps.
        if (Math.abs(targetTopPx - scrollEl.scrollTop) <= scrollEl.clientHeight * 4) {
          smoothWriteScrollTop(targetTopPx, "scrollbar-physical");
        } else {
          cancelSmoothScroll();
          writeScrollTop(scrollEl, targetTopPx, "scrollbar-physical");
        }
        pendingJumpIndex = null;
        if (geometry !== null) {
          const anchor = anchorForUser();
          if (anchor !== null) {
            // Gap judged at the drag TARGET, not the mid-glide position.
            const bottomGapPx = scrollEl.scrollHeight - targetTopPx - scrollEl.clientHeight;
            const totalEvents = dataSource.getTotalEvents();
            const atTail =
              bottomGapPx < BOTTOM_THRESHOLD_PX &&
              spacerBottomPx <= 0 &&
              (totalEvents === null || extent().endIndex >= totalEvents);
            // Same deferred-landing semantics as onScrollEvent: a drag ending
            // at an edge while the fill lags lands exactly there once loaded.
            pendingTailIntent = isTrackEndIntent || bottomGapPx < BOTTOM_THRESHOLD_PX;
            pendingTopIntent = isTrackStartIntent || targetTopPx < BOTTOM_THRESHOLD_PX;
            dispatchPosition({ kind: "USER_SCROLLED", source: "scrollbar", anchor, atTail });
          }
        }
      } else {
        const { firstIndex, endIndex } = extent();
        const isTargetLoaded = target.index >= firstIndex && target.index < endIndex;
        const rowIndex =
          isTargetLoaded && geometry !== null && geometryRows.length > 0
            ? rowIndexForEventIndex(rowEventIndexes, target.index)
            : -1;
        if (rowIndex >= 0 && geometry !== null) {
          // The index is already loaded (an index-space resolution through a
          // stale frozen band): position directly onto its row -- no jump, no
          // overlay, the content for that spot is on hand.
          pendingJumpIndex = null;
          const targetTopPx = spacerTopPx + geometry.rowTops[rowIndex];
          if (Math.abs(targetTopPx - scrollEl.scrollTop) <= scrollEl.clientHeight * 4) {
            smoothWriteScrollTop(targetTopPx, "scrollbar-index");
          } else {
            cancelSmoothScroll();
            writeScrollTop(scrollEl, targetTopPx, "scrollbar-index");
          }
          const anchor = anchorForUser();
          if (anchor !== null) {
            const bottomGapPx = scrollEl.scrollHeight - targetTopPx - scrollEl.clientHeight;
            const totalEvents = dataSource.getTotalEvents();
            const atTail =
              bottomGapPx < BOTTOM_THRESHOLD_PX &&
              spacerBottomPx <= 0 &&
              (totalEvents === null || endIndex >= totalEvents);
            pendingTailIntent = isTrackEndIntent || bottomGapPx < BOTTOM_THRESHOLD_PX;
            pendingTopIntent = isTrackStartIntent || targetTopPx < BOTTOM_THRESHOLD_PX;
            dispatchPosition({ kind: "USER_SCROLLED", source: "scrollbar", anchor, atTail });
          }
        } else {
          // A virtual-region target: give immediate feedback by moving into the
          // spacer (the loading overlay covers it) and let the fill planner land
          // the window there; the JUMPED_TO_INDEX dispatch anchors on landing.
          pendingJumpIndex = target.index;
          const totalEvents = dataSource.getTotalEvents() ?? endIndex;
          if (target.index < firstIndex && firstIndex > 0 && spacerTopPx > 0) {
            writeScrollTop(scrollEl, (target.index / firstIndex) * spacerTopPx, "scrollbar-virtual");
          } else if (target.index >= endIndex && totalEvents > endIndex && spacerBottomPx > 0) {
            const intoFraction = (target.index - endIndex) / (totalEvents - endIndex);
            writeScrollTop(
              scrollEl,
              spacerTopPx + physicalHeightPx() + intoFraction * spacerBottomPx,
              "scrollbar-virtual",
            );
          }
          // Leave FOLLOW now, not on landing. An unloaded target near the
          // window fills in via fetch-before/after (no fetch-at-offset, so no
          // JUMPED_TO_INDEX dispatch ever fires), and while FOLLOW holds the
          // pin drags the viewport straight back to the bottom -- the jump
          // silently never happens. The current-view anchor holds the spacer
          // position while the fill lands.
          // A target at either extreme records edge intent, so the landing
          // (which anchors the target row at the viewport TOP) gets corrected
          // to the exact edge by runAfterRender once that edge is loaded.
          pendingTailIntent = isTrackEndIntent || (totalEvents > 0 && target.index >= totalEvents - 1);
          pendingTopIntent = isTrackStartIntent || target.index <= 0;
          if (positionState.kind === "FOLLOW" && geometry !== null) {
            const anchor = anchorForUser();
            if (anchor !== null) {
              dispatchPosition({ kind: "USER_SCROLLED", source: "scrollbar", anchor, atTail: false });
            }
          }
          planFill();
        }
      }
      m.redraw();
    },

    scrollbarRelease(): void {
      lastActivityAtMs = performance.now();
      lastScrollbarFraction = null;
      frozenThumbSizeFraction = null;
      m.redraw();
    },

    getScrollbarRenderState(): ScrollbarRenderState {
      const totalEvents = dataSource.getTotalEvents() ?? 0;
      const hasTrack = geometry !== null && (geometryRows.length > 0 || totalEvents > 0);
      if (!hasTrack) {
        return { thumbStartFraction: 0, thumbSizeFraction: 1, isActive: false, hasTrack: false };
      }
      const isActive = performance.now() - lastActivityAtMs < SCROLLBAR_SHOW_MS;
      if (scrollbarState.kind === "SCROLLBAR" && lastScrollbarFraction !== null && frozenThumbSizeFraction !== null) {
        // While the user works the scrollbar, the thumb follows their pointer
        // through the frozen mapping and keeps its engage-time size. Visibility
        // stays time-based: the SCROLLBAR state outlives the pointer release
        // (only another interaction clears it), and the bar must still fade in
        // a quiescent view.
        const sizeFraction = frozenThumbSizeFraction;
        return {
          thumbStartFraction: lastScrollbarFraction * (1 - sizeFraction),
          thumbSizeFraction: sizeFraction,
          isActive,
          hasTrack: true,
        };
      }
      const thumb = computeThumb(currentMapping(), viewportNow(), physicalHeightPx());
      return {
        thumbStartFraction: thumb.startFraction,
        thumbSizeFraction: thumb.sizeFraction,
        isActive,
        hasTrack: true,
      };
    },
  };
}
