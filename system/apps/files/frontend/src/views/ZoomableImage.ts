/**
 * A picture that zooms and moves: a pinch on a trackpad or a touch screen (or Ctrl+scroll) zooms around the pointer,
 * a drag or a scroll moves a zoomed picture, and a double-click zooms in, or back out to the fitted picture. A zoomed
 * picture is kept covering the view (or centred along a side where it is smaller), and a fitted one can be dragged
 * out of the window as any picture can.
 */

import Panzoom, { type PanzoomObject } from "@panzoom/panzoom";
import m from "mithril";

/** How far past its fitted size a picture zooms in. */
export const MAX_IMAGE_ZOOM = 8;

/** The zoom a double-click on a fitted picture goes to. */
export const DOUBLE_CLICK_ZOOM = 2;

/** How much a Ctrl+wheel delta changes the zoom, as an exponent per unit. */
const PINCH_ZOOM_RATE = 0.01;

/** The largest Ctrl+wheel delta one event counts for, so a mouse wheel's notch is a step rather than a leap. */
const MAX_PINCH_DELTA = 25;

interface ZoomableImageAttrs {
  src: string;
  alt: string;
  /** The classes of the frame the picture sits in, which clips it while it is zoomed. */
  frameClass: string;
}

interface Pan {
  x: number;
  y: number;
}

/** The pan nearest ``pan`` that keeps a picture ``imageSize`` across, zoomed by ``scale`` around the middle of a view
 *  ``viewSize`` across, covering the view, or centred in it when it is smaller. Pans are in the stage's own pixels. */
export function panWithinView(pan: Pan, scale: number, imageSize: Pan, viewSize: Pan): Pan {
  const within = (value: number, image: number, view: number): number => {
    const bound = Math.max(0, (scale * image - view) / (2 * scale));
    return Math.min(bound, Math.max(-bound, value));
  };
  return { x: within(pan.x, imageSize.x, viewSize.x), y: within(pan.y, imageSize.y, viewSize.y) };
}

/** Make ``stage``, which fills ``frame`` and centres ``image``, zoom and move inside it; the returned function undoes
 *  it. */
export function attachZoom(frame: HTMLElement, stage: HTMLElement, image: HTMLImageElement): () => void {
  const zoom: PanzoomObject = Panzoom(stage, {
    minScale: 1,
    maxScale: MAX_IMAGE_ZOOM,
    panOnlyWhenZoomed: true,
    noBind: true,
    cursor: "default",
    handleStartEvent: (event: Event) => event.preventDefault(),
  });
  const settle = (animate: boolean): void => {
    const pan = zoom.getPan();
    const scale = zoom.getScale();
    const target = panWithinView(
      pan,
      scale,
      { x: image.offsetWidth, y: image.offsetHeight },
      { x: stage.clientWidth, y: stage.clientHeight },
    );
    if (Math.abs(target.x - pan.x) > 0.5 || Math.abs(target.y - pan.y) > 0.5) {
      zoom.pan(target.x, target.y, { force: true, animate });
    }
  };
  const zoomAround = (scale: number, point: { clientX: number; clientY: number }): void => {
    if (scale <= 1) zoom.reset({ animate: false });
    else zoom.zoomToPoint(Math.min(scale, MAX_IMAGE_ZOOM), point, { animate: false });
    settle(false);
  };
  const onWheel = (event: WheelEvent): void => {
    const scale = zoom.getScale();
    if (event.ctrlKey) {
      event.preventDefault();
      const delta = Math.min(MAX_PINCH_DELTA, Math.max(-MAX_PINCH_DELTA, event.deltaY));
      zoomAround(scale * Math.exp(-delta * PINCH_ZOOM_RATE), event);
    } else if (scale > 1) {
      event.preventDefault();
      zoom.pan(-event.deltaX / scale, -event.deltaY / scale, { relative: true, animate: false });
      settle(false);
    }
  };
  const onDoubleClick = (event: MouseEvent): void => {
    if (zoom.getScale() > 1) zoom.reset();
    else {
      zoom.zoomToPoint(DOUBLE_CLICK_ZOOM, event);
      settle(false);
    }
  };
  const onPointerDown = (event: PointerEvent): void => {
    if (event.button !== 0) return;
    // A fitted picture leaves a mouse press to the browser, so the picture can be dragged out; a touch always starts
    // a gesture, since a pinch begins from the fitted picture.
    if (event.pointerType === "mouse" && zoom.getScale() <= 1) return;
    zoom.handleDown(event);
  };
  const onGestureEnd = (): void => settle(true);
  const onChange = (): void => {
    const isZoomed = zoom.getScale() > 1;
    zoom.setOptions({ cursor: isZoomed ? "grab" : "default" });
    image.draggable = !isZoomed;
  };
  frame.addEventListener("wheel", onWheel, { passive: false });
  frame.addEventListener("dblclick", onDoubleClick);
  frame.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", zoom.handleMove);
  document.addEventListener("pointerup", zoom.handleUp);
  document.addEventListener("pointercancel", zoom.handleUp);
  stage.addEventListener("panzoomend", onGestureEnd);
  stage.addEventListener("panzoomchange", onChange);
  return () => {
    frame.removeEventListener("wheel", onWheel);
    frame.removeEventListener("dblclick", onDoubleClick);
    frame.removeEventListener("pointerdown", onPointerDown);
    document.removeEventListener("pointermove", zoom.handleMove);
    document.removeEventListener("pointerup", zoom.handleUp);
    document.removeEventListener("pointercancel", zoom.handleUp);
    stage.removeEventListener("panzoomend", onGestureEnd);
    stage.removeEventListener("panzoomchange", onChange);
    zoom.destroy();
  };
}

export function ZoomableImage(): m.Component<ZoomableImageAttrs> {
  let detach: (() => void) | null = null;
  return {
    oncreate({ dom }) {
      const frame = dom as HTMLElement;
      const stage = frame.firstElementChild as HTMLElement;
      detach = attachZoom(frame, stage, stage.firstElementChild as HTMLImageElement);
    },
    onremove() {
      detach?.();
    },
    view({ attrs: { src, alt, frameClass } }) {
      return m(
        "div.files-image-frame",
        { class: `${frameClass} overflow-hidden` },
        m(
          "div.files-image-stage",
          { class: "flex h-full w-full items-center justify-center" },
          m("img.files-file-image", { src, alt, class: "max-h-full max-w-full object-contain" }),
        ),
      );
    },
  };
}
