/**
 * Custom-scrollbar track math: the physical region maps in pixel space (exact),
 * the virtual regions on each end map in index space -- each side's track share
 * is its share of the event count, so clicking 70% of the way into a virtual
 * region lands on the event 70% of the way through it.
 *
 * `computeLiveMapping` builds the mapping for the ELSEWHERE state; the SCROLLBAR
 * state freezes one of these at engage time and keeps resolving through it.
 */

import type { PhysicalExtent, ScrollTarget, ScrollbarMapping, TrackSegment, Viewport, VirtualExtent } from "./types";

export interface ScrollbarThumb {
  readonly startFraction: number;
  readonly sizeFraction: number;
}

export function computeLiveMapping(
  virtualExtent: VirtualExtent,
  physical: PhysicalExtent,
  physicalContentHeightPx: number,
): ScrollbarMapping {
  const totalEvents = virtualExtent.totalEvents;
  const loadedCount = Math.max(0, physical.endIndex - physical.firstIndex);
  if (totalEvents <= 0) {
    return {
      segments: [{ kind: "physical", trackStart: 0, trackEnd: 1, heightPx: physicalContentHeightPx }],
      totalEvents: 0,
    };
  }
  if (loadedCount <= 0) {
    return {
      segments: [{ kind: "virtual", trackStart: 0, trackEnd: 1, firstIndex: 0, endIndex: totalEvents }],
      totalEvents,
    };
  }

  // Track shares by event count: [0, first/total] virtual, [first/total,
  // end/total] physical, [end/total, 1] virtual, dropping empty sides.
  const physicalTrackStart = physical.firstIndex / totalEvents;
  const physicalTrackEnd = physical.endIndex / totalEvents;
  const segments: TrackSegment[] = [];
  if (physical.firstIndex > 0) {
    segments.push({
      kind: "virtual",
      trackStart: 0,
      trackEnd: physicalTrackStart,
      firstIndex: 0,
      endIndex: physical.firstIndex,
    });
  }
  segments.push({
    kind: "physical",
    trackStart: physical.firstIndex > 0 ? physicalTrackStart : 0,
    trackEnd: physical.endIndex < totalEvents ? physicalTrackEnd : 1,
    heightPx: physicalContentHeightPx,
  });
  if (physical.endIndex < totalEvents) {
    segments.push({
      kind: "virtual",
      trackStart: physicalTrackEnd,
      trackEnd: 1,
      firstIndex: physical.endIndex,
      endIndex: totalEvents,
    });
  }
  return { segments, totalEvents };
}

/** Resolve a track position (0..1) to a scroll target through a mapping. */
export function resolveTrackFraction(mapping: ScrollbarMapping, fraction: number): ScrollTarget {
  const clamped = Math.min(1, Math.max(0, fraction));
  // Segments are contiguous and ascending. Boundaries belong to the later
  // segment (start-inclusive), so the physical band's start resolves to its top
  // pixel; the last segment catches fraction = 1 (and skips zero-width ones).
  let segment = mapping.segments[mapping.segments.length - 1];
  for (const candidate of mapping.segments) {
    if (clamped < candidate.trackEnd) {
      segment = candidate;
      break;
    }
  }
  const width = segment.trackEnd - segment.trackStart;
  const relative = width > 0 ? (clamped - segment.trackStart) / width : 0;
  if (segment.kind === "physical") {
    return { kind: "physical-px", contentTopPx: relative * segment.heightPx };
  }
  const count = segment.endIndex - segment.firstIndex;
  const indexWithin = Math.min(count - 1, Math.floor(relative * count));
  return { kind: "virtual-index", index: segment.firstIndex + Math.max(0, indexWithin) };
}

// A scroll-space pixel position mapped to a track fraction. Each segment's
// scroll-space extent: virtual-before-physical <-> the top spacer, physical <->
// the physical content, virtual-after <-> the bottom spacer (spacers are linear
// in index by construction, so px fraction == index fraction within a spacer).
function trackFractionForScrollSpacePx(
  mapping: ScrollbarMapping,
  scrollSpacePx: number,
  viewport: Viewport,
  physicalContentHeightPx: number,
): number {
  const hasPhysical = mapping.segments.some((segment) => segment.kind === "physical");
  let extentStartPx = 0;
  let isBeforePhysical = true;
  for (let i = 0; i < mapping.segments.length; i++) {
    const segment = mapping.segments[i];
    let extentPx: number;
    if (segment.kind === "physical") {
      extentPx = physicalContentHeightPx;
      isBeforePhysical = false;
    } else if (!hasPhysical) {
      extentPx = viewport.spacerTopPx + physicalContentHeightPx + viewport.spacerBottomPx;
    } else {
      extentPx = isBeforePhysical ? viewport.spacerTopPx : viewport.spacerBottomPx;
    }
    const isLastSegment = i === mapping.segments.length - 1;
    if (scrollSpacePx <= extentStartPx + extentPx || isLastSegment) {
      const relative = extentPx > 0 ? (scrollSpacePx - extentStartPx) / extentPx : 0;
      const clampedRelative = Math.min(1, Math.max(0, relative));
      return segment.trackStart + clampedRelative * (segment.trackEnd - segment.trackStart);
    }
    extentStartPx += extentPx;
  }
  return 1;
}

/** Thumb position/size on the track for the current viewport (ELSEWHERE rendering). */
export function computeThumb(
  mapping: ScrollbarMapping,
  viewport: Viewport,
  physicalContentHeightPx: number,
): ScrollbarThumb {
  const startFraction = trackFractionForScrollSpacePx(
    mapping,
    viewport.scrollTopPx,
    viewport,
    physicalContentHeightPx,
  );
  const endFraction = trackFractionForScrollSpacePx(
    mapping,
    viewport.scrollTopPx + viewport.heightPx,
    viewport,
    physicalContentHeightPx,
  );
  return { startFraction, sizeFraction: Math.max(0, endFraction - startFraction) };
}
