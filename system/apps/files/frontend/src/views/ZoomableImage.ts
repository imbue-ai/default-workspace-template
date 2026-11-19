/**
 * A picture that zooms and moves with Panzoom's own gestures: a pinch (a trackpad's reaches the page as Ctrl+wheel)
 * or Ctrl+scroll zooms around the pointer, a drag moves a zoomed picture, and the picture always covers its frame.
 */

import Panzoom from "@panzoom/panzoom";
import m from "mithril";

/** The most a picture zooms in, as a multiple of its fitted size. */
const MAX_IMAGE_ZOOM = 8;

interface ZoomableImageAttrs {
  src: string;
  alt: string;
  /** The classes of the frame the picture sits in, which clips it while it is zoomed; it takes no padding, since
   *  the zoomed stage covers the frame's whole box. */
  frameClass: string;
}

export function ZoomableImage(): m.Component<ZoomableImageAttrs> {
  let detach: (() => void) | null = null;
  return {
    oncreate({ dom }) {
      const frame = dom as HTMLElement;
      const stage = frame.firstElementChild as HTMLElement;
      const zoom = Panzoom(stage, {
        minScale: 1,
        maxScale: MAX_IMAGE_ZOOM,
        contain: "outside",
        // A press keeps its default, so it still focuses the page and the shell raises a window clicked behind others.
        handleStartEvent: () => undefined,
      });
      const onWheel = (event: WheelEvent): void => {
        if (event.ctrlKey) zoom.zoomWithWheel(event);
      };
      // A touch pinch back to the fitted zoom can stop a few pixels off centre, which `contain` does not take back.
      const onGestureEnd = (): void => {
        if (zoom.getScale() <= 1) zoom.reset();
      };
      frame.addEventListener("wheel", onWheel);
      stage.addEventListener("panzoomend", onGestureEnd);
      detach = () => {
        frame.removeEventListener("wheel", onWheel);
        stage.removeEventListener("panzoomend", onGestureEnd);
        zoom.destroy();
      };
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
          { class: "flex h-full w-full items-center justify-center p-6" },
          m("img.files-file-image", { src, alt, draggable: false, class: "max-h-full max-w-full object-contain" }),
        ),
      );
    },
  };
}
