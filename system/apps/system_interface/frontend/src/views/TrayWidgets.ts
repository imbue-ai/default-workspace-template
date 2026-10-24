/**
 * The widgets a client put in the taskbar (app-widgets plan section 4.10): a slot per widget, in the tray beside the
 * desktop switcher or in the bar's row beside the launcher, where it sits as a window's entry does, the size its
 * page last reported for its form there; and above the bar the popover of the one whose page was clicked, the size
 * its page reports expanded, over the slot's end of the bar. Each is only a box: the widget frames layer lays the
 * widget's one frame over the slot, or over the popover's content box while it is open, so the page moves between
 * them without reloading. The popover is the desktop widget's panel, with no rim to drag; a click elsewhere or
 * Escape closes it. Nothing here measures the DOM or listens for gestures.
 */

import m from "mithril";
import type { PixelSize } from "../geometry/frames";
import type { AppRecord } from "../model/records";
import type { TaskbarSpot } from "../reducers/desktopState";
import { WIDGET_ATTRIBUTE, WIDGET_CONTENT_ATTRIBUTE, WIDGET_POPOVER_ATTRIBUTE } from "../pages/widgetFrames";

/** A widget's slot in the taskbar's tray, which carries its app. */
export const TRAY_WIDGET_ATTRIBUTE = "data-tray-widget";
/** A widget's slot in the taskbar's row, which carries its app. */
export const TOOLBAR_WIDGET_ATTRIBUTE = "data-toolbar-widget";

/** One widget in the taskbar: its app and the size its page reported for its form there. */
export interface TrayWidget {
  readonly app: AppRecord;
  readonly size: PixelSize;
}

export interface TrayWidgetSlotsAttrs {
  readonly spot: TaskbarSpot;
  readonly widgets: readonly TrayWidget[];
  readonly popoverApp: string | null;
  /** A click on a slot its page does not cover (while its popover is open): its popover closes. */
  readonly onPress: (app: string) => void;
  /** A right-click on a slot its page does not cover (while its popover is open): the widget's menu. */
  readonly onContextMenu: (app: string, x: number, y: number, target: Element) => void;
}

/** The slots at one spot: inside the system tray's row, or in the bar's row, where they take a window entry's
 *  height and its tints. */
export const TrayWidgetSlots: m.Component<TrayWidgetSlotsAttrs> = {
  view(vnode) {
    const { spot, widgets, popoverApp, onPress, onContextMenu } = vnode.attrs;
    const isOpen = (app: string): boolean => popoverApp === app;
    return widgets.map(({ app, size }) =>
      m(
        "div",
        {
          key: app.name,
          [WIDGET_ATTRIBUTE]: app.name,
          [spot === "tray" ? TRAY_WIDGET_ATTRIBUTE : TOOLBAR_WIDGET_ATTRIBUTE]: app.name,
          "data-popover-open": isOpen(app.name) ? "true" : "false",
          class:
            (spot === "tray"
              ? "tray-widget max-h-(--desk-widget-tray-height) max-w-(--desk-widget-tray-max-width) " +
                (isOpen(app.name) ? "bg-fill-hover " : "hover:bg-fill-hover ")
              : "toolbar-widget max-h-(--desk-taskbar-entry-size) max-w-(--desk-widget-toolbar-max-width) " +
                (isOpen(app.name) ? "bg-fill-active " : "hover:bg-fill-hover ")) +
            "flex shrink-0 overflow-hidden rounded-md",
          style: { width: `${size.width}px`, height: `${size.height}px` },
          onclick: (event: MouseEvent) => {
            if (event.button === 0) onPress(app.name);
          },
          oncontextmenu: (event: MouseEvent) => {
            event.preventDefault();
            onContextMenu(app.name, event.clientX, event.clientY, event.target as Element);
          },
        },
        m("div", { [WIDGET_CONTENT_ATTRIBUTE]: "", class: "min-h-0 min-w-0 flex-1" }),
      ),
    );
  },
};

export interface TrayWidgetPopoverAttrs {
  readonly app: string;
  /** Which end of the bar it opens over: the tray's (right), or the row's start (left), where its slot is. */
  readonly spot: TaskbarSpot;
  /** The size its page reported expanded, clamped to the theme's widget maximum. */
  readonly size: PixelSize;
}

/** The open popover, above the bar: at its right end for the tray, or over the start of the box holding the row's
 *  slots, which reaches the bar's top. */
export const TrayWidgetPopover: m.Component<TrayWidgetPopoverAttrs> = {
  view(vnode) {
    const { app, spot, size } = vnode.attrs;
    return m(
      "div",
      {
        [WIDGET_POPOVER_ATTRIBUTE]: app,
        class:
          "tray-widget-popover absolute bottom-full z-(--z-sticky) mb-2 flex overflow-hidden " +
          (spot === "tray" ? "right-2 " : "left-0 ") +
          "rounded-(--desk-widget-radius) border border-(--desk-widget-edge) bg-(--desk-widget-surface) " +
          "p-(--desk-widget-rim) shadow-raised backdrop-blur-xl",
      },
      m("div", {
        [WIDGET_CONTENT_ATTRIBUTE]: "",
        class: "min-h-0 min-w-0",
        style: { width: `${size.width}px`, height: `${size.height}px` },
      }),
    );
  },
};
