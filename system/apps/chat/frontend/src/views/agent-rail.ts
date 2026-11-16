/**
 * The line that threads a stretch of the agent's output: a 1px rule down the bubbles' text edge, through the
 * centres of the chip icons, step bullets, notice ticks and the working dot, from each piece of the stretch to the
 * next. It is drawn only in the gaps between pieces and stops short of each by that piece's air, so it never shows
 * through a bubble or an icon. A stretch is one row of the transcript, so the line never crosses a person's turn or
 * a switch to another agent.
 *
 * A piece on the line marks itself with `data-rail-node` (see {@link railNodeAttrs}). The line is measured from the
 * laid-out pieces rather than drawn by them, so it follows whatever they do -- a chip row wrapping, a chip's panel
 * opening, a step unfurling its work -- with no piece knowing what sits next to it.
 */

import m from "mithril";

/** What a piece on the line is, which says where the line meets it and how far short it stops:
 *   - box: a bubble or a card, met at its edge
 *   - chips: a chip row, met as a whole, on its first chip's icon
 *   - step: a timeline bullet (a tk step, a handoff)
 *   - point: a small mark (a notice's tick, the working dot, the wait ring) */
export type RailNodeKind = "box" | "chips" | "step" | "point";

const RAIL_NODE_KINDS: ReadonlySet<string> = new Set<RailNodeKind>(["box", "chips", "step", "point"]);

/** How far short of a piece the line stops: past a chip's hover fill, around a step bullet as far as a chip's hover
 *  clears its icon, around a small mark's glyph. A box's edge is its own gap. */
const RAIL_AIR_PX: Readonly<Record<RailNodeKind, number>> = { box: 0, chips: 2, step: 5.5, point: 4 };

/** Where the line runs in a stretch with only boxes to place it by: a bubble's padding plus half an icon's width. */
const BOX_LINE_OFFSET_PX = 22.5;

/** How far off the line a mark's centre may sit and still be on it. A mark further off is nested deeper (a chip in a
 *  step's unfurled work) and the line passes it by. */
const ON_LINE_TOLERANCE_PX = 3;

/** The element inside a piece whose centre the line runs through: a chip's icon, or a mark that stands in for one. */
const RAIL_ICON_SELECTOR = ".tool-chip-icon, [data-rail-icon]";

/** The attributes that put a piece on the line. */
export function railNodeAttrs(kind: RailNodeKind): { "data-rail-node": RailNodeKind } {
  return { "data-rail-node": kind };
}

/** A piece on the line, in the stretch's own coordinates. */
export interface RailNode {
  readonly kind: RailNodeKind;
  readonly top: number;
  readonly bottom: number;
  readonly left: number;
  /** The horizontal centre of the mark the line runs through (the element's own, for a box). */
  readonly centerX: number;
}

export interface RailSegment {
  readonly x: number;
  readonly top: number;
  readonly height: number;
}

/** The line's segments for a stretch's pieces, top to bottom: one in each gap between consecutive pieces on the line,
 *  each stopping short of both ends by their air. */
export function railSegments(nodes: readonly RailNode[]): RailSegment[] {
  const first = nodes.find((node) => node.kind !== "box") ?? nodes[0];
  if (first === undefined) return [];
  const x = first.kind === "box" ? first.left + BOX_LINE_OFFSET_PX : first.centerX;
  const onLine = nodes.filter((node) => node.kind === "box" || Math.abs(node.centerX - x) < ON_LINE_TOLERANCE_PX);
  const segments: RailSegment[] = [];
  for (let i = 1; i < onLine.length; i++) {
    const above = onLine[i - 1];
    const below = onLine[i];
    const top = above.bottom + RAIL_AIR_PX[above.kind];
    const height = below.top - RAIL_AIR_PX[below.kind] - top;
    if (height > 1) segments.push({ x: Math.round(x - 0.5), top, height });
  }
  return segments;
}

/** The pieces on the line inside `host`, in document order (which, down a column, is top to bottom). */
function measureRailNodes(host: HTMLElement): RailNode[] {
  const origin = host.getBoundingClientRect();
  const nodes: RailNode[] = [];
  for (const element of host.querySelectorAll<HTMLElement>("[data-rail-node]")) {
    const kind = element.dataset.railNode ?? "";
    if (!RAIL_NODE_KINDS.has(kind)) continue;
    const rect = element.getBoundingClientRect();
    if (rect.height === 0) continue;
    const mark = element.querySelector(RAIL_ICON_SELECTOR)?.getBoundingClientRect() ?? rect;
    // A chip row starts at its first chip, whose hover fill is what the air clears; it ends at its foot, below any
    // panel a chip has open.
    const firstChip = kind === "chips" ? element.querySelector(".tool-chip")?.getBoundingClientRect() : undefined;
    nodes.push({
      kind: kind as RailNodeKind,
      top: (firstChip?.top ?? rect.top) - origin.top,
      bottom: rect.bottom - origin.top,
      left: rect.left - origin.left,
      centerX: mark.left + mark.width / 2 - origin.left,
    });
  }
  return nodes;
}

/** The line over its parent (the stretch's column), redrawn whenever the column redraws or changes size. It writes
 *  its own segments rather than rendering them, so measuring never triggers another redraw. */
export function AgentRail(): m.Component {
  let observer: ResizeObserver | null = null;
  let drawnSignature = "";

  const draw = (rail: Element): void => {
    const host = rail.parentElement;
    if (host === null || !host.isConnected) return;
    const segments = railSegments(measureRailNodes(host));
    const signature = JSON.stringify(segments);
    if (signature === drawnSignature) return;
    drawnSignature = signature;
    rail.replaceChildren(
      ...segments.map((segment) => {
        const piece = document.createElement("div");
        piece.className = "agent-rail-segment absolute w-px bg-default";
        piece.style.left = `${segment.x}px`;
        piece.style.top = `${segment.top}px`;
        piece.style.height = `${segment.height}px`;
        return piece;
      }),
    );
  };

  return {
    oncreate(vnode) {
      draw(vnode.dom);
      const host = vnode.dom.parentElement;
      if (host !== null && typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(() => draw(vnode.dom));
        observer.observe(host);
      }
    },
    onupdate(vnode) {
      draw(vnode.dom);
    },
    onremove() {
      observer?.disconnect();
      observer = null;
    },
    view() {
      return m("div", { class: "agent-rail pointer-events-none absolute inset-0", "aria-hidden": "true" });
    },
  };
}
