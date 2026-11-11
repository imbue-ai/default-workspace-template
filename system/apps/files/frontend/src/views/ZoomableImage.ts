/**
 * A picture that zooms and moves: a pinch on a trackpad or a touch screen (or Ctrl+scroll) zooms around the pointer,
 * a drag or a scroll moves a zoomed picture, and a double-click zooms in, or back out to the fitted picture.
 */

import Panzoom, { type PanzoomObject } from "@panzoom/panzoom";
import m from "mithril";

/** How far past its fitted size a picture zooms in. */
export const MAX_IMAGE_ZOOM = 8;

/** The zoom a double-click on a fitted picture goes to. */
export const DOUBLE_CLICK_ZOOM = 2;

/** How much a pinch's wheel delta changes the zoom, as an exponent per unit: a trackpad pinch sends many small ones. */
const PINCH_ZOOM_RATE = 0.01;

interface ZoomableImageAttrs {
  src: string;
  alt: string;
  /** The classes of the frame the picture sits in, which clips it while it is zoomed. */
  frameClass: string;
}

/** Make ``stage``, which fills ``frame`` and centres the picture, zoom and move inside it; the returned function
 *  undoes it. */
export function attachZoom(frame: HTMLElement, stage: HTMLElement): () => void {
  const zoom: PanzoomObject = Panzoom(stage, {
    minScale: 1,
    maxScale: MAX_IMAGE_ZOOM,
    panOnlyWhenZoomed: true,
    noBind: true,
    cursor: "default",
    handleStartEvent: (event: Event) => event.preventDefault(),
  });
  const zoomAround = (scale: number, point: { clientX: number; clientY: number }): void => {
    if (scale <= 1) zoom.reset({ animate: false });
    else zoom.zoomToPoint(Math.min(scale, MAX_IMAGE_ZOOM), point, { animate: false });
  };
  const onWheel = (event: WheelEvent): void => {
    const scale = zoom.getScale();
    if (event.ctrlKey) {
      event.preventDefault();
      zoomAround(scale * Math.exp(-event.deltaY * PINCH_ZOOM_RATE), event);
    } else if (scale > 1) {
      event.preventDefault();
      zoom.pan(-event.deltaX / scale, -event.deltaY / scale, { relative: true, animate: false });
    }
  };
  const onDoubleClick = (event: MouseEvent): void => {
    if (zoom.getScale() > 1) zoom.reset();
    else zoom.zoomToPoint(DOUBLE_CLICK_ZOOM, event);
  };
  const onPointerDown = (event: PointerEvent): void => {
    if (event.button === 0) zoom.handleDown(event);
  };
  const onChange = (): void => {
    zoom.setOptions({ cursor: zoom.getScale() > 1 ? "grab" : "default" });
  };
  frame.addEventListener("wheel", onWheel, { passive: false });
  frame.addEventListener("dblclick", onDoubleClick);
  frame.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("pointermove", zoom.handleMove);
  document.addEventListener("pointerup", zoom.handleUp);
  document.addEventListener("pointercancel", zoom.handleUp);
  stage.addEventListener("panzoomchange", onChange);
  return () => {
    frame.removeEventListener("wheel", onWheel);
    frame.removeEventListener("dblclick", onDoubleClick);
    frame.removeEventListener("pointerdown", onPointerDown);
    document.removeEventListener("pointermove", zoom.handleMove);
    document.removeEventListener("pointerup", zoom.handleUp);
    document.removeEventListener("pointercancel", zoom.handleUp);
    stage.removeEventListener("panzoomchange", onChange);
    zoom.destroy();
  };
}

export function ZoomableImage(): m.Component<ZoomableImageAttrs> {
  let detach: (() => void) | null = null;
  return {
    oncreate({ dom }) {
      const frame = dom as HTMLElement;
      detach = attachZoom(frame, frame.firstElementChild as HTMLElement);
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
          m("img.files-file-image", { src, alt, draggable: false, class: "max-h-full max-w-full object-contain" }),
        ),
      );
    },
  };
}
