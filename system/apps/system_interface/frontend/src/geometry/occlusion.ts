/**
 * Whether a window's live page is hidden behind the windows in front of it, so the shell can stop the browser
 * rendering it. A wrong "covered" leaves a hole where the user expects the page, so every approximation here
 * errs towards "not covered".
 *
 * A window is a rounded rectangle (``windowRadius`` at each corner). Its title bar is always opaque; its content
 * area is opaque only while its own page (or the stopped-app placeholder) fills it, and is otherwise see-through.
 * A front window covers with its rectangle less the corner squares, which keeps its rounded corners out of the
 * cover. A page is its window's content box with the window's two bottom corners rounded, so its own corner
 * squares need covering only where no front window rounds the same corner at the same point.
 */

import type { PixelRect, PixelSize } from "./frames";

/** A window in front of the page under test: where it is drawn, and whether its content area paints. */
export interface FrontWindow {
  readonly rect: PixelRect;
  readonly isContentOpaque: boolean;
}

export interface OcclusionMetrics {
  readonly titleBarHeight: number;
  readonly windowRadius: number;
}

/** Strips thinner than this are rounding between the store's rectangles and the DOM's, not visible page. */
const SLIVER_PX = 0.5;

/** Whether the page of the window drawn at ``window`` is wholly hidden, within the backdrop, behind ``front``. */
export function isPageCovered(
  window: PixelRect,
  front: readonly FrontWindow[],
  backdrop: PixelSize,
  metrics: OcclusionMetrics,
): boolean {
  const radius = metrics.windowRadius;
  const content: PixelRect = {
    x: window.x,
    y: window.y + metrics.titleBarHeight,
    width: window.width,
    height: window.height - metrics.titleBarHeight,
  };
  const visible = intersection(content, { x: 0, y: 0, width: backdrop.width, height: backdrop.height });
  if (visible === null) return true;
  const covers = front.flatMap((other) => opaqueParts(other, metrics));
  const bottom = window.y + window.height;
  const left = window.x;
  const right = window.x + window.width;
  for (const corner of [
    { x: left, isLeft: true },
    { x: right, isLeft: false },
  ]) {
    if (front.some((other) => roundsSameBottomCorner(other, corner.x, bottom, corner.isLeft))) {
      covers.push({ x: corner.isLeft ? left : right - radius, y: bottom - radius, width: radius, height: radius });
    }
  }
  return isRectCovered(visible, covers);
}

/** The rectangles of a window that are certainly painted: its rectangle less the four corner squares when its
 *  content is opaque, else the title bar less its two top corner squares. */
function opaqueParts(window: FrontWindow, metrics: OcclusionMetrics): PixelRect[] {
  const { x, y, width } = window.rect;
  const radius = metrics.windowRadius;
  const height = window.isContentOpaque ? window.rect.height : Math.min(metrics.titleBarHeight, window.rect.height);
  const bottomInset = window.isContentOpaque ? radius : 0;
  return [
    { x, y: y + radius, width, height: height - radius - bottomInset },
    { x: x + radius, y, width: width - 2 * radius, height },
  ].filter((rect) => rect.width > 0 && rect.height > 0);
}

/** Whether ``window`` paints a rounded bottom corner at (``cornerX``, ``bottom``) on the given side. */
function roundsSameBottomCorner(window: FrontWindow, cornerX: number, bottom: number, isLeft: boolean): boolean {
  if (!window.isContentOpaque) return false;
  const { rect } = window;
  const x = isLeft ? rect.x : rect.x + rect.width;
  return Math.abs(x - cornerX) < SLIVER_PX && Math.abs(rect.y + rect.height - bottom) < SLIVER_PX;
}

function intersection(a: PixelRect, b: PixelRect): PixelRect | null {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  const width = Math.min(a.x + a.width, b.x + b.width) - x;
  const height = Math.min(a.y + a.height, b.y + b.height) - y;
  return width > 0 && height > 0 ? { x, y, width, height } : null;
}

/** Whether ``target`` lies inside the union of ``covers``: every cell of the grid their edges cut ``target``
 *  into is wholly inside or wholly outside each cover, so testing one point per cell is exact. */
function isRectCovered(target: PixelRect, covers: readonly PixelRect[]): boolean {
  const clipped = covers.flatMap((cover) => intersection(cover, target) ?? []);
  const xs = edges(
    target.x,
    target.width,
    clipped.map((cover) => [cover.x, cover.x + cover.width]),
  );
  const ys = edges(
    target.y,
    target.height,
    clipped.map((cover) => [cover.y, cover.y + cover.height]),
  );
  for (let i = 0; i + 1 < xs.length; i += 1) {
    if (xs[i + 1] - xs[i] < SLIVER_PX) continue;
    const midX = (xs[i] + xs[i + 1]) / 2;
    for (let j = 0; j + 1 < ys.length; j += 1) {
      if (ys[j + 1] - ys[j] < SLIVER_PX) continue;
      const midY = (ys[j] + ys[j + 1]) / 2;
      const isInside = clipped.some(
        (cover) => midX > cover.x && midX < cover.x + cover.width && midY > cover.y && midY < cover.y + cover.height,
      );
      if (!isInside) return false;
    }
  }
  return true;
}

function edges(start: number, length: number, spans: readonly number[][]): number[] {
  return [...new Set([start, start + length, ...spans.flat()])].sort((a, b) => a - b);
}
