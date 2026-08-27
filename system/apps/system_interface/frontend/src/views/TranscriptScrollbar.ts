/**
 * Custom overlay scrollbar for transcript views (the native one is hidden --
 * see .transcript-scroll in style.css). A slim auto-hiding track on the right
 * edge; the thumb position/size come from the engine's mapping (pixel space
 * over the loaded window, index space over unloaded history).
 *
 * Interaction contract (spec: transcript-smooth-scroll.md): pressing engages
 * the engine's SCROLLBAR state (freezing the mapping); dragging keeps
 * resolving pointer fractions through that frozen mapping. Releasing does NOT
 * unfreeze -- only interacting with something else does (the engine handles
 * that). Native grab feel: pressing ON the thumb keeps the pointer's offset
 * into it (no jump on press); pressing the empty track jumps to that spot and
 * the drag continues from there with the pointer centered on the thumb.
 */

import m from "mithril";
import { SCROLLBAR_SHOW_MS, type TranscriptScrollEngine } from "./transcript-scroll-engine";

// Minimum thumb height so it stays grabbable on very long transcripts.
const MIN_THUMB_PX = 24;

interface TranscriptScrollbarAttrs {
  engine: TranscriptScrollEngine;
}

export function TranscriptScrollbar(): m.Component<TranscriptScrollbarAttrs> {
  let trackEl: HTMLElement | null = null;
  let isDragging = false;
  let isHovered = false;
  // Pointer offset into the thumb at grab time, so a drag moves the thumb
  // relative to where it was grabbed instead of teleporting it to the pointer.
  let grabOffsetPx = 0;
  // Auto-hide: the engine's isActive is a time window with nothing at its end,
  // and mithril only redraws on events -- without a scheduled redraw the faded
  // state would never render in a quiescent view, leaving the bar visible.
  let hideRedrawTimer: ReturnType<typeof setTimeout> | null = null;

  function thumbElement(): HTMLElement | null {
    return trackEl?.querySelector<HTMLElement>(".transcript-scrollbar-thumb") ?? null;
  }

  /** The track fraction that puts the grabbed point of the thumb under the pointer. */
  function fractionForDrag(event: PointerEvent): number {
    const thumbEl = thumbElement();
    if (trackEl === null || thumbEl === null) {
      return 0;
    }
    const trackRect = trackEl.getBoundingClientRect();
    const usablePx = trackRect.height - thumbEl.getBoundingClientRect().height;
    if (usablePx <= 0) {
      return 0;
    }
    const thumbTopPx = event.clientY - trackRect.top - grabOffsetPx;
    return Math.min(1, Math.max(0, thumbTopPx / usablePx));
  }

  return {
    onremove() {
      if (hideRedrawTimer !== null) {
        clearTimeout(hideRedrawTimer);
        hideRedrawTimer = null;
      }
    },

    view(vnode) {
      const engine = vnode.attrs.engine;
      const state = engine.getScrollbarRenderState();
      if (!state.hasTrack) {
        return null;
      }
      const isShown = state.isActive || isHovered || isDragging;
      if (state.isActive && hideRedrawTimer === null) {
        // If activity continued, the redraw sees isActive still true and this
        // re-schedules, so the fade lands within 2x the window of the last touch.
        hideRedrawTimer = setTimeout(() => {
          hideRedrawTimer = null;
          m.redraw();
        }, SCROLLBAR_SHOW_MS);
      }

      const thumbSizePercent = state.thumbSizeFraction * 100;
      // The engine reports start = f * (1 - sizeFraction); recover the track
      // fraction f and position against the RENDERED thumb height (which the
      // 24px minimum can enlarge), so the thumb spans exactly [0, track - thumb]
      // and never overflows the track bottom on very long transcripts.
      const trackFraction = state.thumbSizeFraction < 1 ? state.thumbStartFraction / (1 - state.thumbSizeFraction) : 0;

      return m(
        "div",
        {
          class: `transcript-scrollbar${isShown ? " transcript-scrollbar-shown" : ""}`,
          oncreate: (trackVnode: m.VnodeDOM) => {
            trackEl = trackVnode.dom as HTMLElement;
          },
          onremove: () => {
            trackEl = null;
          },
          onmouseenter: () => {
            isHovered = true;
          },
          onmouseleave: () => {
            isHovered = false;
          },
          onpointerdown: (event: PointerEvent) => {
            event.preventDefault();
            isDragging = true;
            (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
            engine.scrollbarEngage();
            const thumbRect = thumbElement()?.getBoundingClientRect() ?? null;
            if (thumbRect !== null && event.clientY >= thumbRect.top && event.clientY <= thumbRect.bottom) {
              // Grabbed the thumb: hold the pointer's offset into it, no jump.
              grabOffsetPx = event.clientY - thumbRect.top;
            } else {
              // Pressed the empty track: jump there, then drag with the
              // pointer centered on the thumb.
              grabOffsetPx = (thumbRect?.height ?? 0) / 2;
              engine.scrollbarMoveTo(fractionForDrag(event));
            }
          },
          onpointermove: (event: PointerEvent) => {
            if (isDragging) {
              engine.scrollbarMoveTo(fractionForDrag(event));
            }
          },
          onpointerup: (event: PointerEvent) => {
            isDragging = false;
            (event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
          },
          onpointercancel: () => {
            isDragging = false;
          },
        },
        m("div", {
          class: "transcript-scrollbar-thumb",
          style:
            `top: calc(${trackFraction} * (100% - max(${thumbSizePercent}%, ${MIN_THUMB_PX}px))); ` +
            `height: max(${thumbSizePercent}%, ${MIN_THUMB_PX}px);`,
        }),
      );
    },
  };
}
