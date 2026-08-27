# Transcript smooth scroll: layers, state machines, custom scrollbar

Supersedes the scroll portions of [chat-scroll-and-selection-bugs.md](chat-scroll-and-selection-bugs.md). All paths are relative to `system/apps/system_interface/frontend/src/`.

## Overview

- The chat scroll-jump bug class comes from estimated geometry feeding native scroll behavior: phantom spacers sized at a constant 160 px/event vs ~34 px/event rendered for tool-heavy turns, so page landings shift geometry by thousands of px, defeat native scroll anchoring, and hard-snap follow state.
- Replace the whole mechanism with three explicit layers and two strict state machines, one shared engine for ChatPanel and SubagentView.
- Layers: **Visible** (mounted rows: viewport + overscan), **Physical** (up to 50,000 contiguous events in memory with exactly measured heights), **Virtual** (the full server transcript, known only by count).
- Scroll position machine: `FOLLOW` (pinned to tail) / `USER_CONTROLLED` (anchored to a row key + px offset). Scrollbar interaction machine: `ELSEWHERE` (live mapping) / `SCROLLBAR` (frozen mapping).
- The app owns anchoring end-to-end: `overflow-anchor: none`, all programmatic scrollTop writes are tagged and invisible to the state machines, and every redraw in `USER_CONTROLLED` re-derives scrollTop from the anchor. Native wheel/touch physics stay native; the native scrollbar is hidden and replaced with a custom overlay scrollbar.
- Chromium-first. Client-only: the existing `/events` API (`before`/`after`/`offset`/`limit` + `{offset, total}`) suffices; any gap found gets flagged, not silently worked around.
- Delivery: a stack of 3 PRs. The first implementation artifact is the TypeScript types below, reviewed before any further code.

## Expected behavior

- Opening a chat paints the tail page instantly (as today), then the physical layer grows in the background (~500 around the user, then ~2000-event chunks) until 50k events or the whole chat is loaded. Eviction re-centers the physical window on the user's location.
- While `FOLLOW`: new events keep the viewport pinned to the bottom. Streaming never jitters the viewport.
- Any upward intent — wheel/touchpad, scrollbar, keyboard (PageUp/Up/Home), selection-drag autoscroll — enters `USER_CONTROLLED`. The anchor is the rendered row CONTAINING the viewport top (the top message on screen), plus its px offset into the row (zero or negative). Anchoring the spanning row rather than the next one is what keeps the top message immobile when its own height corrects — a measurement replacing an estimate, or an expand/collapse — since those shift only rows after it. (A ProgressBlock turn is one anchor unit.)
- While `USER_CONTROLLED`: the anchored row does not move on screen, no matter what lands — streamed events, backfill pages, offscreen measurements, spacer re-estimates. Scrolling to the true bottom (nothing newer unloaded) or sending a message returns to `FOLLOW`.
- Wheel/non-scrollbar input never repositions content programmatically; it only updates the custom scrollbar to match the screen.
- Custom overlay scrollbar (auto-hide, right edge): the physical region maps in pixel space (exact), the virtual regions on each end map in index space — clicking at 70% of a virtual region lands on the event 70% of the way through it (thumb drags identical; track clicks jump to position).
- Scrollbar interaction freezes the track mapping (`SCROLLBAR` state): repeated scrubbing keeps using the mapping captured at grab time, even as loads land. Any other interaction returns to `ELSEWHERE`, where the mapping resizes/moves live.
- Virtual end spacers exist in scroll space only while unloaded history remains, sized by the measured physical average px/event with live smoothing — applied only in `ELSEWHERE`, always paired with an exact compensating scrollTop write so nothing visibly moves.
- A deep wheel fling into a spacer extends physical at the edge; beyond a gap threshold it re-centers around the implied index in one fetch — landing smoothly, never moving backwards. A jump into an unloaded region shows the existing centered "Loading messages..." overlay until the target mounts.
- Scrollbar jumps land exactly at the target (index in virtual regions, pixel in physical).
- Per-agent scroll state (`FOLLOW` or anchor) persists to localStorage and restores across app reload; a vanished anchor row falls back to `FOLLOW`.
- Live text selections survive: while a selection is active inside the transcript, unmounting and eviction freeze (window grows only) — the disjoint pinned-run machinery is gone.
- Panel width changes fully invalidate measured heights; visible rows re-measure immediately, the offscreen pass re-runs, the anchor holds the reading position.
- SubagentView behaves identically with an empty virtual layer (its transcript is fully loaded): same engine, same scrollbar (100% physical), same states.
- `?debug=scroll` traces state transitions, anchor + offset, and every compensation write into a ring buffer exposed on `window` (JSON-dumpable), with optional console echo.
- Not in scope, noted as follow-ups in the PR description: a "jump to latest" pill, a thumb-drag position tooltip.

## Implementation plan

### Core types (the first deliverable, for review before any other code)

New module `models/transcriptScroll/types.ts` (names final after review):

> **As built** (the listing below is the reviewed plan; `models/transcriptScroll/types.ts` is the authority):
> `ScrollTarget`'s physical arm became `physical-fraction` (a fraction through the physical region, which the
> engine scales over the region's scrollable span) instead of `physical-px`/`contentTopPx`;
> `PersistedScrollState` gained `anchorEventIndex` so a restore can re-center the fill (the `/events` API
> addresses by offset, not row key); `resolveTrackFraction`/`computeThumb` take the physical content height
> rather than `PhysicalExtent` + `PhysicalGeometry`; and the fill constants settled at
> `INITIAL_TAIL_LIMIT=50`, `JUMP_WINDOW_LIMIT=500`, `FILL_CHUNK_LIMIT=2000` (see `fillPlanner.ts`).

```ts
// --- Layer vocabulary ------------------------------------------------------

/** Global position of an event in the virtual transcript: 0 <= index < totalEvents. */
export type EventIndex = number;

/** Stable identity of a rendered row (an event_id, or "progress-<turnKey>"). */
export type RowKey = string;

/** Virtual layer: what exists on the server. The client only ever knows the count. */
export interface VirtualExtent {
  readonly totalEvents: number;
}

/** Physical layer: the contiguous event window held in memory. */
export interface PhysicalExtent {
  readonly firstIndex: EventIndex; // inclusive
  readonly endIndex: EventIndex;   // exclusive; endIndex - firstIndex = loaded count
}

/** Exact geometry of the physical rows (offscreen-measured). */
export interface PhysicalGeometry {
  readonly rowKeys: readonly RowKey[];   // derived rows, in transcript order
  readonly rowTops: readonly number[];   // prefix sums (px); exact once measured
  readonly totalHeightPx: number;
  readonly unmeasuredCount: number;      // > 0 only transiently after a fill lands
}

/** Live scroll-space facts read from the DOM each frame. */
export interface Viewport {
  readonly scrollTopPx: number;
  readonly heightPx: number;
  readonly spacerTopPx: number;    // current virtual end spacer sizes
  readonly spacerBottomPx: number;
}

// --- State machine 1: scroll position --------------------------------------

/** Anchor: the row containing the viewport top (the top message on screen). */
export interface ScrollAnchor {
  readonly rowKey: RowKey;
  /** rowTop - viewportTop, px; zero or negative while the viewport top is inside the row. */
  readonly offsetPx: number;
}

export type ScrollPositionState =
  | { readonly kind: "FOLLOW" }
  | { readonly kind: "USER_CONTROLLED"; readonly anchor: ScrollAnchor };

/** Every input that can express scroll intent is first-class. */
export type ScrollInputSource = "wheel" | "keyboard" | "scrollbar" | "selection-autoscroll";

export type ScrollPositionEvent =
  /**
   * The user moved the viewport (any source). `anchor` is freshly computed from the
   * DOM; `atTail` is true only at the true bottom with nothing newer unloaded.
   * Programmatic writes (follow pins, compensation) and browser shrink-clamps are
   * tagged by the engine and NEVER produce this event -- the reducer only ever sees
   * genuine input, which is the no-jitter guarantee.
   */
  | { readonly kind: "USER_SCROLLED"; readonly source: ScrollInputSource; readonly anchor: ScrollAnchor; readonly atTail: boolean }
  /** New events appended by streaming (no user input). */
  | { readonly kind: "EVENTS_APPENDED" }
  /** The user submitted a message from the composer. */
  | { readonly kind: "MESSAGE_SENT" }
  /** A scrollbar jump landed in a virtual region and mounted its target window. */
  | { readonly kind: "JUMPED_TO_INDEX"; readonly anchor: ScrollAnchor };

/** Pure, exhaustive (compile-time `never` check on both unions). */
export function reduceScrollPosition(state: ScrollPositionState, event: ScrollPositionEvent): ScrollPositionState;
// FOLLOW          + USER_SCROLLED(atTail=false) -> USER_CONTROLLED(anchor)   [any intent to scroll up]
// FOLLOW          + EVENTS_APPENDED             -> FOLLOW                     [new messages]
// USER_CONTROLLED + USER_SCROLLED(atTail=true)  -> FOLLOW                     [scrolled all the way down]
// USER_CONTROLLED + USER_SCROLLED(atTail=false) -> USER_CONTROLLED(new anchor)
// USER_CONTROLLED + MESSAGE_SENT                -> FOLLOW                     [send snaps to tail]
// *               + JUMPED_TO_INDEX             -> USER_CONTROLLED(anchor)
// everything else: identity

// --- State machine 2: scrollbar interaction --------------------------------

/** One segment of the custom scrollbar track; fractions of track length in [0, 1]. */
export type TrackSegment =
  | {
      // Unloaded history: mapped in index (percent) space.
      readonly kind: "virtual";
      readonly trackStart: number;
      readonly trackEnd: number;
      readonly firstIndex: EventIndex; // inclusive
      readonly endIndex: EventIndex;   // exclusive
    }
  | {
      // The loaded window: mapped in pixel space, exact.
      readonly kind: "physical";
      readonly trackStart: number;
      readonly trackEnd: number;
      readonly heightPx: number; // physical content height at mapping time
    };

/** Full track: [virtual?] physical [virtual?], contiguous, covering [0, 1]. */
export interface ScrollbarMapping {
  readonly segments: readonly TrackSegment[];
  readonly totalEvents: number;
}

export type ScrollbarInteractionState =
  | { readonly kind: "ELSEWHERE" }                                    // mapping recomputed live
  | { readonly kind: "SCROLLBAR"; readonly frozen: ScrollbarMapping }; // mapping frozen at grab

export type ScrollbarInteractionEvent =
  | { readonly kind: "SCROLLBAR_ENGAGED"; readonly mappingAtEngage: ScrollbarMapping }
  | { readonly kind: "OTHER_INTERACTION" }; // wheel/keyboard/pointer anywhere else, typing, send

export function reduceScrollbarInteraction(
  state: ScrollbarInteractionState,
  event: ScrollbarInteractionEvent,
): ScrollbarInteractionState;
// ELSEWHERE + SCROLLBAR_ENGAGED -> SCROLLBAR(mappingAtEngage)
// SCROLLBAR + SCROLLBAR_ENGAGED -> SCROLLBAR(unchanged frozen mapping)  [keep scrubbing]
// SCROLLBAR + OTHER_INTERACTION -> ELSEWHERE
// ELSEWHERE + OTHER_INTERACTION -> ELSEWHERE

// --- Scrollbar resolution ---------------------------------------------------

/** What a track position resolves to through a mapping. */
export type ScrollTarget =
  | { readonly kind: "physical-px"; readonly contentTopPx: number }  // exact px in physical content
  | { readonly kind: "virtual-index"; readonly index: EventIndex };  // requires load/jump

export function resolveTrackFraction(mapping: ScrollbarMapping, fraction: number): ScrollTarget;
export function computeLiveMapping(virtualExtent: VirtualExtent, physical: PhysicalExtent, geometry: PhysicalGeometry): ScrollbarMapping;
export function computeThumb(
  mapping: ScrollbarMapping,
  viewport: Viewport,
  physical: PhysicalExtent,
  geometry: PhysicalGeometry,
): { readonly startFraction: number; readonly sizeFraction: number };

// --- Persistence ------------------------------------------------------------

export interface PersistedScrollState {
  readonly version: 1;
  readonly state: ScrollPositionState; // anchor rowKey validated on restore; else FOLLOW
}
```

### New modules (`models/transcriptScroll/`, all DOM-free and unit-tested)

- `types.ts` — the types above.
- `state.ts` — the two reducers, exhaustive over both unions.
- `geometry.ts` — build `PhysicalGeometry` from rows + height table (prefix sums); `anchorFromViewport(geometry, viewport)` (find first row at/below viewport top); `scrollTopForAnchor(geometry, anchor, spacerTopPx)` (the compensation target); visible-window computation (viewport ± overscan → row index range) replacing `computeVisibleWindow`.
- `scrollbarMap.ts` — `computeLiveMapping`, `resolveTrackFraction`, `computeThumb`; pure math per the percent/pixel split.
- `spacerEstimate.ts` — avg px/event from measured physical rows with live smoothing (applied only in `ELSEWHERE`; each application returns the paired scrollTop delta).
- `fillPlanner.ts` — progressive-fill decisions: given physical extent, anchor/tail location, virtual total, and the 50k cap, return the next fetch (`direction`, `offset`, `limit`) or eviction (which side, how much). Drives tail-first then ~500 → ~2000-event chunks, re-centering on the user.
- `persistence.ts` — localStorage codec (`transcript-scroll:<agentId>`), versioned, anchor-validation on restore.
- `trace.ts` — ring buffer + console echo behind `?debug=scroll`; entries for reducer transitions, anchor resolution, compensation writes, spacer applications, fill decisions.
- `rowEventIndex.ts` — (as-built addition) the bridge between rendered rows and global event indexes: each row's starting event index (a turn collapses many events into one row), and the row containing a given event index. Used by scroll persistence, the fill planner's focus, and jump landing.

### New view modules (`views/`)

- `transcript-scroll-engine.ts` — the shared DOM-side engine replacing `transcript-scroll.ts`. Owns: native scroll/wheel/keyboard/pointer listeners; tagging of programmatic scrollTop writes (a write counter — scroll events observed while writes are pending are consumed, never reduced); per-redraw positioning (`FOLLOW`: pin bottom; `USER_CONTROLLED`: write `scrollTopForAnchor`); the offscreen measurer; selection freeze gate (`isSelectionActiveWithin` survives); width-change full invalidation; wiring of both reducers plus the fill planner to the store.
- `TranscriptScrollbar.ts` — the custom overlay scrollbar component: hidden native scrollbar (`scrollbar-width: none`), absolute track/thumb, auto-hide on idle, pointer capture for drags, track-click jump-to-position, driven by `ScrollbarMapping`/`computeThumb`; engages/releases the interaction machine.
- `offscreen-measure.ts` — hidden same-width container inside the panel; renders not-yet-measured physical rows in idle batches (budgeted ms/frame) via the existing lazy `render()` closures; feeds the height table; re-queues everything on width invalidation. Replaces `row-measurement.ts` measurement-by-mounting (a slim mounted-row measurer remains for live streaming rows).

### Modified

- `models/Response.ts` — `TranscriptStore` stays the physical store: raise the cap to the 50k budget, expose extent/total to the planner, let the engine drive eviction (re-centering) instead of the fixed `MAX_HELD_EVENTS`/`EVICT_TARGET_EVENTS` policy; fetch helpers gain caller-specified limits for chunked fill. Contiguity guard, dedup, in-place upgrade, `renderVersion` all survive unchanged.
- `views/ChatPanel.ts` — drops phantom geometry, `maybePage`, jump/pin logic, spacer math; consumes the engine + scrollbar; adds `MESSAGE_SENT` dispatch from the composer path and persistence restore on agent switch.
- `views/SubagentView.ts` — same consumption with an empty virtual layer.
- `views/conversation-rows.ts` — row building survives as-is; `renderTranscriptSegments` simplifies to top spacer / one row run / bottom spacer (no disjoint pinned runs).

### Deleted (PR3)

- `views/transcript-scroll.ts`, `models/virtualWindow.ts` (+ tests), `models/scrollFollow.ts`'s `nextUserScrolledUp` (+ tests; `isSelectionActiveWithin` moves next to the engine), `views/row-measurement.ts`, `views/scroll-selection.ts` pinned-run resolution, `ESTIMATED_*_HEIGHT_PX` constants and phantom-region comments in ChatPanel.

## Implementation phases

Stacked PRs; each phase is a working system.

- **Phase 1 — types first, then the pure engine (PR1).** Step 1 is `types.ts` exactly as above, presented for review; nothing else is built until the types are agreed. Then `state.ts`, `geometry.ts`, `scrollbarMap.ts`, `spacerEstimate.ts`, `fillPlanner.ts`, `persistence.ts`, `trace.ts`, all with vitest coverage. No view changes; the app still runs on the old machinery.
- **Phase 2 — ChatPanel on the new engine (PR2).** `transcript-scroll-engine.ts`, `TranscriptScrollbar.ts`, `offscreen-measure.ts`; `Response.ts` store changes; ChatPanel migrated (progressive fill, custom scrollbar, persistence, debug trace, selection freeze, send-snaps-to-FOLLOW). Old modules still exist for SubagentView. Manual verification gate before merge.
- **Phase 3 — SubagentView + deletion (PR3).** SubagentView migrated; every superseded module and its tests deleted; `chat-scroll-and-selection-bugs.md` gets a superseded-by note. PR description records the follow-ups (jump-to-latest pill, drag tooltip).

## Testing strategy

- **Unit (vitest, `frontend && npm test`):** exhaustive transition tables for both reducers (every state × event); anchor round-trips (`anchorFromViewport` ∘ `scrollTopForAnchor` = identity on exact geometry); scrollbar mapping (fraction→target→fraction round-trips, percent/pixel boundary continuity, frozen-mapping stability); spacer smoothing always returns a compensation equal to its scrollHeight delta; fill planner (tail-first, chunk growth, 50k cap, re-centering, eviction sides); persistence codec (restore, version/row-key fallback to FOLLOW).
- **Manual (Playwright scripts, not CI):** re-use the session-15136f8d repro approach — standalone system-interface server + the real 1514-line tool-heavy fixture; drive wheel events and scrollbar drags; assert via the `?debug=scroll` ring buffer (content-relative positions, not scrollTop). Must-pass: no jitter wheel-scrolling during streaming; no backwards jumps ever; anchored row pixel-stable through backfill, measurement, and spacer re-estimates. Also: scrub-freeze behavior, jump-accuracy into virtual regions, reload restore, selection survival, width-resize hold.
- **Edge cases:** chat shorter than the viewport; empty chat; chat exactly at the 50k cap; anchor row evicted server-side before restore; scrollbar grabbed during an in-flight fill; fling into a spacer during initial fill; subagent (no virtual layer) path.

## Open questions

- Smoothing function and constants for the spacer estimate (EMA factor, application cadence) — tune freely during Phase 2, finals documented in the PR.
- Offscreen measurement CPU budget (ms per idle frame) and batch size — tune freely.
- Whether keyboard scrolling needs an explicit `tabindex` on the scroll container for focus, or hover-scroll suffices in the Electron shell — resolve in Phase 2.
- Exact fetch sizes (500 initial / 2000 chunks) vs parse-time measurements on huge transcripts — tune freely.
