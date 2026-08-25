/**
 * Core types for the transcript scroll engine (see
 * docs/system/specs/transcript-smooth-scroll.md).
 *
 * Three layers: Visible (mounted rows), Physical (the contiguous event window
 * held in memory with exactly measured heights), Virtual (the full server
 * transcript, known only by count). Two state machines: scroll position
 * (FOLLOW / USER_CONTROLLED) and scrollbar interaction (ELSEWHERE / SCROLLBAR).
 * Everything here is plain data -- the reducers and math live in sibling modules.
 */

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
  readonly endIndex: EventIndex; // exclusive; endIndex - firstIndex = loaded count
}

/** Exact geometry of the physical rows (offscreen-measured). */
export interface PhysicalGeometry {
  readonly rowKeys: readonly RowKey[]; // derived rows, in transcript order
  readonly rowTops: readonly number[]; // prefix sums (px); exact once measured
  readonly totalHeightPx: number;
  readonly unmeasuredCount: number; // > 0 only transiently after a fill lands
}

/** Live scroll-space facts read from the DOM each frame. */
export interface Viewport {
  readonly scrollTopPx: number;
  readonly heightPx: number;
  readonly spacerTopPx: number; // current virtual end spacer sizes
  readonly spacerBottomPx: number;
}

// --- State machine 1: scroll position --------------------------------------

/** Anchor: the first row whose top edge sits at/below the viewport top. */
export interface ScrollAnchor {
  readonly rowKey: RowKey;
  /** rowTop - viewportTop, px; small by construction (less than that row's height). */
  readonly offsetPx: number;
}

export type ScrollPositionState =
  | { readonly kind: "FOLLOW" }
  | { readonly kind: "USER_CONTROLLED"; readonly anchor: ScrollAnchor };

/** Every input that can express scroll intent is first-class. */
export type ScrollInputSource = "wheel" | "keyboard" | "scrollbar" | "selection-autoscroll";

export type ScrollPositionEvent =
  /**
   * The user moved the viewport (any source). `anchor` is freshly computed from
   * the DOM; `atTail` is true only at the true bottom with nothing newer
   * unloaded. Programmatic writes (follow pins, compensation) and browser
   * shrink-clamps are tagged by the engine and NEVER produce this event -- the
   * reducer only ever sees genuine input, which is the no-jitter guarantee.
   */
  | {
      readonly kind: "USER_SCROLLED";
      readonly source: ScrollInputSource;
      readonly anchor: ScrollAnchor;
      readonly atTail: boolean;
    }
  /** New events appended by streaming (no user input). */
  | { readonly kind: "EVENTS_APPENDED" }
  /** The user submitted a message from the composer. */
  | { readonly kind: "MESSAGE_SENT" }
  /** A scrollbar jump landed in a virtual region and mounted its target window. */
  | { readonly kind: "JUMPED_TO_INDEX"; readonly anchor: ScrollAnchor };

// --- State machine 2: scrollbar interaction --------------------------------

/** One segment of the custom scrollbar track; fractions of track length in [0, 1]. */
export type TrackSegment =
  | {
      // Unloaded history: mapped in index (percent) space.
      readonly kind: "virtual";
      readonly trackStart: number;
      readonly trackEnd: number;
      readonly firstIndex: EventIndex; // inclusive
      readonly endIndex: EventIndex; // exclusive
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
  | { readonly kind: "ELSEWHERE" } // mapping recomputed live
  | { readonly kind: "SCROLLBAR"; readonly frozen: ScrollbarMapping }; // mapping frozen at grab

export type ScrollbarInteractionEvent =
  | { readonly kind: "SCROLLBAR_ENGAGED"; readonly mappingAtEngage: ScrollbarMapping }
  | { readonly kind: "OTHER_INTERACTION" }; // wheel/keyboard/pointer anywhere else, typing, send

// --- Scrollbar resolution ---------------------------------------------------

/** What a track position resolves to through a mapping. */
export type ScrollTarget =
  | { readonly kind: "physical-px"; readonly contentTopPx: number } // exact px in physical content
  | { readonly kind: "virtual-index"; readonly index: EventIndex }; // requires load/jump

// --- Persistence ------------------------------------------------------------

export interface PersistedScrollState {
  readonly version: 1;
  readonly state: ScrollPositionState; // anchor rowKey validated on restore; else FOLLOW
  /**
   * Global index of the event the anchor row starts at, captured at persist
   * time. A restore centers the initial physical fill here -- row keys alone
   * cannot be located on the server (the /events API addresses by offset, not
   * id), so without this a mid-history anchor could never be re-loaded.
   */
  readonly anchorEventIndex: EventIndex | null;
}
