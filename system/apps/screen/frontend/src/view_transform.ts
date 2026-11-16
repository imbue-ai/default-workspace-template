/**
 * Where the video is drawn inside the stage and how a client point maps onto the Screen: the intrinsic aspect is
 * kept, letterboxed and centred (docs/system/specs/screen/spec.md section 7.2), and on a phone a zoom and a pan
 * move the drawn video (section 14). The transform is the CSS ``translate(pan) scale(zoom)`` applied to the fitted
 * video about the stage's top-left corner.
 */

export interface Size {
  width: number;
  height: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface Rect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The viewer's own zoom and pan over the fitted video; identity on a desktop. */
export interface ViewTransform {
  zoom: number;
  pan: Point;
}

export const IDENTITY_TRANSFORM: ViewTransform = { zoom: 1, pan: { x: 0, y: 0 } };
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 5;

/** The video's box inside the stage with no zoom: fitted to the stage at its intrinsic aspect and centred. */
export function fittedVideoRect(stage: Size, screen: Size): Rect | null {
  if (screen.width <= 0 || screen.height <= 0 || stage.width <= 0 || stage.height <= 0) return null;
  const scale = Math.min(stage.width / screen.width, stage.height / screen.height);
  const width = screen.width * scale;
  const height = screen.height * scale;
  return { left: (stage.width - width) / 2, top: (stage.height - height) / 2, width, height };
}

/** A point in stage coordinates (relative to the stage's top-left) mapped onto the Screen, or null outside it. */
export function screenPointFromStagePoint(
  point: Point,
  stage: Size,
  screen: Size,
  transform: ViewTransform = IDENTITY_TRANSFORM,
): Point | null {
  const fitted = fittedVideoRect(stage, screen);
  if (fitted === null) return null;
  // Undo the pan and the zoom (both about the stage origin), then the letterbox fit.
  const unzoomedX = (point.x - transform.pan.x) / transform.zoom;
  const unzoomedY = (point.y - transform.pan.y) / transform.zoom;
  const scale = fitted.width / screen.width;
  const x = Math.round((unzoomedX - fitted.left) / scale);
  const y = Math.round((unzoomedY - fitted.top) / scale);
  if (x < 0 || y < 0 || x >= screen.width || y >= screen.height) return null;
  return { x, y };
}

/** A client point (viewport coordinates) mapped onto the Screen, given the stage's bounding rect. */
export function screenPointFromClient(
  clientX: number,
  clientY: number,
  stageRect: Rect,
  screen: Size,
  transform: ViewTransform = IDENTITY_TRANSFORM,
): Point | null {
  return screenPointFromStagePoint(
    { x: clientX - stageRect.left, y: clientY - stageRect.top },
    { width: stageRect.width, height: stageRect.height },
    screen,
    transform,
  );
}

/** The transform kept within bounds: zoom in [1, 5], and the drawn video never leaves a gap at an edge it could
 *  cover (at zoom 1 the pan is zero). */
export function clampTransform(transform: ViewTransform, stage: Size): ViewTransform {
  const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, transform.zoom));
  const minX = stage.width - stage.width * zoom;
  const minY = stage.height - stage.height * zoom;
  return {
    zoom,
    pan: {
      x: Math.min(0, Math.max(minX, transform.pan.x)),
      y: Math.min(0, Math.max(minY, transform.pan.y)),
    },
  };
}

/** Zoom by a factor about a stage point, keeping that point under the finger. */
export function zoomAbout(transform: ViewTransform, factor: number, about: Point, stage: Size): ViewTransform {
  const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, transform.zoom * factor));
  const applied = zoom / transform.zoom;
  return clampTransform(
    {
      zoom,
      pan: {
        x: about.x - (about.x - transform.pan.x) * applied,
        y: about.y - (about.y - transform.pan.y) * applied,
      },
    },
    stage,
  );
}

export function panBy(transform: ViewTransform, delta: Point, stage: Size): ViewTransform {
  return clampTransform(
    { zoom: transform.zoom, pan: { x: transform.pan.x + delta.x, y: transform.pan.y + delta.y } },
    stage,
  );
}

export function transformToCss(transform: ViewTransform): string {
  return `translate(${transform.pan.x}px, ${transform.pan.y}px) scale(${transform.zoom})`;
}
