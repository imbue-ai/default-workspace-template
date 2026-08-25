/**
 * Custom overlay scrollbar for transcript views (the native one is hidden --
 * see .transcript-scroll in style.css). A slim auto-hiding track on the right
 * edge; the thumb position/size come from the engine's mapping (pixel space
 * over the loaded window, index space over unloaded history).
 *
 * Interaction contract (spec: transcript-smooth-scroll.md): pressing anywhere
 * on the track engages the engine's SCROLLBAR state (freezing the mapping) and
 * jumps to the pressed position; dragging keeps resolving pointer fractions
 * through that frozen mapping. Releasing does NOT unfreeze -- only interacting
 * with something else does (the engine handles that).
 */

import m from "mithril";
import type { TranscriptScrollEngine } from "./transcript-scroll-engine";

// Minimum thumb height so it stays grabbable on very long transcripts.
const MIN_THUMB_PX = 24;

interface TranscriptScrollbarAttrs {
  engine: TranscriptScrollEngine;
}

export function TranscriptScrollbar(): m.Component<TranscriptScrollbarAttrs> {
  let trackEl: HTMLElement | null = null;
  let isDragging = false;
  let isHovered = false;

  function fractionForPointer(event: PointerEvent): number {
    if (trackEl === null) {
      return 0;
    }
    const rect = trackEl.getBoundingClientRect();
    if (rect.height <= 0) {
      return 0;
    }
    return (event.clientY - rect.top) / rect.height;
  }

  return {
    view(vnode) {
      const engine = vnode.attrs.engine;
      const state = engine.getScrollbarRenderState();
      if (!state.hasTrack) {
        return null;
      }
      const isShown = state.isActive || isHovered || isDragging;

      const thumbSizePercent = state.thumbSizeFraction * 100;
      const thumbStartPercent = state.thumbStartFraction * 100;

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
            engine.scrollbarMoveTo(fractionForPointer(event));
          },
          onpointermove: (event: PointerEvent) => {
            if (isDragging) {
              engine.scrollbarMoveTo(fractionForPointer(event));
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
          style: `top: ${thumbStartPercent}%; height: ${thumbSizePercent}%; min-height: ${MIN_THUMB_PX}px;`,
        }),
      );
    },
  };
}
