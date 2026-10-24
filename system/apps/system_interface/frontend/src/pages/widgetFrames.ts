/**
 * The widget frames (app-widgets plan sections 4 and 6): one iframe per enabled widget, at the app's widget path,
 * created when the widget is first enabled in this client and destroyed when it is disabled or withdrawn. As the
 * live pages are, a frame is never re-parented (that reloads it); the reconcile step lays each frame over its
 * widget's content box once the chrome is drawn.
 *
 * A frame lives in one of two hosts: the backdrop's, for a widget on the desktop (and every widget on a phone), and
 * the taskbar's, for a widget this client put in the taskbar's tray or its row. A widget moved between the hosts is
 * framed anew in the other. In the taskbar the page is told mode ``tray`` or ``toolbar`` and laid over its slot; while
 * its popover is open the same frame is told ``expanded`` and laid over the popover's box instead, so opening it
 * reloads nothing. A taskbar page's click (``shell:widget-press``) opens or closes its popover, and any widget page's
 * right-click (``shell:widget-menu``) asks for its menu through ``onMenu``.
 *
 * A widget is not drawn until its page reports a size, so a frame the chrome has no box for yet is kept loaded
 * but out of sight, at the largest size a widget may take, where its page can lay itself out and measure.
 *
 * The shell side of the app contract for a widget lives here: the handshake after every load and on a desktop change
 * (``surface: "widget"``, no window), the mode after every handshake and on every change, and the page's own
 * ``shell:widget-size``, ``shell:widget-hidden``, ``shell:widget-drag`` and ``shell:widget-tooltip``, the last shown
 * as the workspace's own tooltip under the element's place in the frame, where the small frame cannot clip it. A
 * drag's points arrive in screen pixels, which stay true while the frame moves under the pointer; the layer turns them
 * into the shell's viewport pixels by where the press was in the frame, and hands each step to ``onDrag``. The rest of
 * what a widget's page may send (an open, a message, a link) the live pages layer takes, asking ``appOfFrame`` whose
 * frame it is. Messages cross through ``relay.ts``.
 */

import {
  SHELL_HANDSHAKE,
  SHELL_WIDGET_DISMISS,
  SHELL_WIDGET_DRAG,
  SHELL_WIDGET_HIDDEN,
  SHELL_WIDGET_MENU,
  SHELL_WIDGET_MODE,
  SHELL_WIDGET_PRESS,
  SHELL_WIDGET_SIZE,
  SHELL_WIDGET_TOOLTIP,
} from "@imbue/workspace-ui/src/app_contract";
import { hideTooltipAtRect, showTooltipAtRect } from "@imbue/workspace-ui/src/components/hoverTooltip";
import type { WidgetMode } from "@imbue/workspace-ui/src/app_contract";
import { windowPageUrl } from "../model/pageUrl";
import type { AppRecord } from "../model/records";
import { isInTaskbar, shownWidgets, taskbarSpotOf } from "../reducers/desktopState";
import type { ShownWidget } from "../reducers/desktopState";
import { sendToChildFrame, setChildFrameMessageHandler } from "../relay";
import type { DesktopStore } from "../store/DesktopStore";
import { PAGE_FRAME_ALLOW, PAGE_FRAME_SANDBOX } from "./livePages";

export const WIDGET_FRAME_ATTRIBUTE = "data-widget-frame";
/** The element of a widget's chrome that carries its app; the layer finds the content box under it. */
export const WIDGET_ATTRIBUTE = "data-widget";
/** The element of a widget's chrome its page is laid over. */
export const WIDGET_CONTENT_ATTRIBUTE = "data-widget-content";
/** A tray widget's open popover, which carries its app; its page is laid over the content box under it. */
export const WIDGET_POPOVER_ATTRIBUTE = "data-widget-popover";

interface WidgetFrame {
  readonly app: string;
  readonly path: string;
  readonly wrapper: HTMLElement;
  readonly frame: HTMLIFrameElement;
  /** The host the frame lives in: the backdrop's or the taskbar's. */
  readonly host: HTMLElement;
  /** The desktop the page was last introduced to; null before its first load. */
  greetedDesktopId: string | null;
  /** The mode the page was last told; null until it has been told one since it loaded. */
  sentMode: WidgetMode | null;
}

/** A point in the shell document's viewport, in CSS pixels. */
export interface ViewportPoint {
  readonly x: number;
  readonly y: number;
}

/** One step of a drag a widget's page reported: where the pointer is now, and at the start where it was pressed. */
export type WidgetDragStep =
  | { readonly phase: "start"; readonly point: ViewportPoint; readonly press: ViewportPoint }
  | { readonly phase: "move" | "end"; readonly point: ViewportPoint }
  | { readonly phase: "cancel" };

export interface WidgetFramesOptions {
  /** The shell document's host and protocol, which every page URL derives from. */
  readonly host: string;
  readonly protocol: string;
  /** A step of a drag a widget's page reported, for the widget of ``app``. */
  readonly onDrag: (app: string, step: WidgetDragStep) => void;
  /** A right-click on the widget of ``app``'s page, at ``point``; ``frame`` is the page's iframe. */
  readonly onMenu: (app: string, point: ViewportPoint, frame: HTMLIFrameElement) => void;
}

/** Who a widget's tooltip belongs to, for the shared bubble. */
function tooltipSource(app: string): string {
  return `widget:${app}`;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export class WidgetFramesLayer {
  private readonly frames = new Map<string, WidgetFrame>();
  private isGestureActive = false;
  /** For each widget its page is dragging: what turns a screen point into a viewport point. */
  private readonly dragOffsets = new Map<string, ViewportPoint>();
  /** The taskbar's host for the tray's frames, while the desktop layout draws a taskbar. */
  private trayHost: HTMLElement | null = null;

  constructor(
    private readonly host: HTMLElement,
    private readonly store: DesktopStore,
    private readonly options: WidgetFramesOptions,
  ) {}

  /** Register the widget messages of the contract. */
  start(): void {
    setChildFrameMessageHandler(SHELL_WIDGET_SIZE, (frame, payload) => this.takeSize(frame, payload));
    setChildFrameMessageHandler(SHELL_WIDGET_HIDDEN, (frame, payload) => this.takeHidden(frame, payload));
    setChildFrameMessageHandler(SHELL_WIDGET_DRAG, (frame, payload) => this.takeDrag(frame, payload));
    setChildFrameMessageHandler(SHELL_WIDGET_TOOLTIP, (frame, payload) => this.takeTooltip(frame, payload));
    setChildFrameMessageHandler(SHELL_WIDGET_PRESS, (frame) => this.takePress(frame));
    setChildFrameMessageHandler(SHELL_WIDGET_DISMISS, (frame) => this.takeDismiss(frame));
    setChildFrameMessageHandler(SHELL_WIDGET_MENU, (frame, payload) => this.takeMenu(frame, payload));
  }

  /** The taskbar's host for the tray's frames, or null once the taskbar is gone; the tray's widgets move to it. */
  setTrayHost(host: HTMLElement | null): void {
    if (this.trayHost === host) return;
    this.trayHost = host;
    this.reconcile();
  }

  /** Stop: every frame goes, and what its page said with it. */
  stop(): void {
    for (const widget of [...this.frames.values()]) this.destroy(widget);
  }

  /** Whether this layer's frames live in ``host``. */
  isOver(host: HTMLElement): boolean {
    return this.host === host;
  }

  /** The app whose widget ``frame`` shows, or null for a frame that is no widget's. */
  appOfFrame(frame: HTMLIFrameElement): string | null {
    const app = frame.getAttribute(WIDGET_FRAME_ATTRIBUTE);
    return app !== null && this.frames.get(app)?.frame === frame ? app : null;
  }

  /** Make every widget inert for the length of a press on a handle, as the live pages are. */
  setGestureActive(isActive: boolean): void {
    if (this.isGestureActive === isActive) return;
    this.isGestureActive = isActive;
    this.reconcile();
  }

  /** Lay one widget's frame over its content box as it is now: the per-move step of its drag. */
  placeFrame(app: string): void {
    const widget = this.frames.get(app);
    if (widget !== undefined) this.place(widget);
  }

  /**
   * Bring the frames in line with the widgets this client shows: a frame per enabled widget (one at a changed
   * path is made anew), each laid over its content box or kept out of sight until the chrome draws one, and
   * every page told the client's mode and active desktop. Run after each redraw.
   */
  reconcile(): void {
    const state = this.store.getState();
    const shown = new Map(shownWidgets(state).map((widget) => [widget.app.name, widget]));
    for (const widget of [...this.frames.values()]) {
      const wanted = shown.get(widget.app);
      if (wanted === undefined || wanted.app.widget?.path !== widget.path || this.hostFor(wanted) !== widget.host) {
        this.destroy(widget);
      }
    }
    for (const shownWidget of shown.values()) {
      const widget = this.frames.get(shownWidget.app.name) ?? this.create(shownWidget);
      if (widget === null) continue;
      this.place(widget);
      if (widget.greetedDesktopId === null) continue;
      if (widget.greetedDesktopId !== (state.activeDesktopId ?? "")) this.greet(widget);
      else if (widget.sentMode !== this.modeFor(shownWidget)) this.sendMode(widget, this.modeFor(shownWidget));
    }
  }

  /** The host a shown widget's frame lives in: the taskbar's for one in the taskbar, else the backdrop's. */
  private hostFor(widget: ShownWidget): HTMLElement {
    return isInTaskbar(widget) && this.trayHost !== null && !this.store.isPhoneLayout() ? this.trayHost : this.host;
  }

  private create(shown: ShownWidget): WidgetFrame | null {
    const app: AppRecord = shown.app;
    if (app.widget === null) return null;
    const host = this.hostFor(shown);
    const wrapper = document.createElement("div");
    wrapper.className = "widget-frame absolute overflow-hidden";
    const frame = document.createElement("iframe");
    frame.setAttribute(WIDGET_FRAME_ATTRIBUTE, app.name);
    frame.setAttribute("sandbox", PAGE_FRAME_SANDBOX);
    frame.setAttribute("allow", PAGE_FRAME_ALLOW);
    frame.title = `${app.display_name} widget`;
    frame.className = "block h-full w-full border-0 bg-transparent";
    wrapper.appendChild(frame);
    const widget: WidgetFrame = {
      app: app.name,
      path: app.widget.path,
      wrapper,
      frame,
      host,
      greetedDesktopId: null,
      sentMode: null,
    };
    // Every load, not just the first: a reload is a fresh page that has said nothing and been told nothing.
    frame.addEventListener("load", () => {
      widget.sentMode = null;
      this.store.resetWidgetReport(widget.app);
      this.greet(widget);
    });
    this.frames.set(app.name, widget);
    host.appendChild(wrapper);
    frame.setAttribute("src", windowPageUrl(app, app.widget.path, this.options.host, this.options.protocol));
    return widget;
  }

  private destroy(widget: WidgetFrame): void {
    this.frames.delete(widget.app);
    hideTooltipAtRect(tooltipSource(widget.app));
    if (this.dragOffsets.delete(widget.app)) this.options.onDrag(widget.app, { phase: "cancel" });
    widget.wrapper.remove();
    this.store.resetWidgetReport(widget.app);
  }

  /** Over the widget's content box when the chrome draws one; else loaded but out of sight, at the largest size a
   *  widget may take, so its page lays itself out and reports. */
  private place(widget: WidgetFrame): void {
    const style = widget.wrapper.style;
    const box = this.contentBox(widget);
    if (box === null) {
      const metrics = this.store.getMetrics();
      Object.assign(style, {
        left: "0px",
        top: "0px",
        width: `${metrics.widgetMaxWidth}px`,
        height: `${metrics.widgetMaxHeight}px`,
        visibility: "hidden",
        pointerEvents: "none",
      });
      return;
    }
    Object.assign(style, {
      left: `${box.left}px`,
      top: `${box.top}px`,
      width: `${box.width}px`,
      height: `${box.height}px`,
      visibility: "",
      pointerEvents: this.isGestureActive ? "none" : "auto",
    });
  }

  /** The box the widget's page is laid over, in its host's pixels: its open popover's, else its chrome's. */
  private contentBox(widget: WidgetFrame): { left: number; top: number; width: number; height: number } | null {
    const app = CSS.escape(widget.app);
    const isPopoverOpen = this.store.getState().trayPopoverApp === widget.app && widget.host === this.trayHost;
    const chrome = isPopoverOpen ? `[${WIDGET_POPOVER_ATTRIBUTE}="${app}"]` : `[${WIDGET_ATTRIBUTE}="${app}"]`;
    const content = widget.host.parentElement?.querySelector<HTMLElement>(`${chrome} [${WIDGET_CONTENT_ATTRIBUTE}]`);
    if (content === undefined || content === null) return null;
    const box = content.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return null;
    const origin = widget.host.getBoundingClientRect();
    return { left: box.left - origin.left, top: box.top - origin.top, width: box.width, height: box.height };
  }

  /** The handshake names the client's active desktop, where anything the widget opens lands; the mode follows. */
  private greet(widget: WidgetFrame): void {
    const state = this.store.getState();
    const desktopId = state.activeDesktopId ?? "";
    sendToChildFrame(widget.frame, SHELL_HANDSHAKE, {
      clientId: state.clientId,
      windowId: null,
      desktopId,
      app: widget.app,
      path: widget.path,
      surface: "widget",
    });
    widget.greetedDesktopId = desktopId;
    const shown = shownWidgets(state).find((candidate) => candidate.app.name === widget.app);
    if (shown !== undefined) this.sendMode(widget, this.modeFor(shown));
  }

  /** The mode a widget is drawn in: on a phone, which draws every widget collapsed, collapsed; in the taskbar, the
   *  form for where it is there, or expanded while its popover is open; else the client's. */
  private modeFor(widget: ShownWidget): WidgetMode {
    if (this.store.isPhoneLayout()) return "collapsed";
    const spot = taskbarSpotOf(widget);
    if (spot !== null && this.hostFor(widget) === this.trayHost) {
      return this.store.getState().trayPopoverApp === widget.app.name ? "expanded" : spot;
    }
    return widget.presentation.mode;
  }

  private sendMode(widget: WidgetFrame, mode: WidgetMode): void {
    widget.sentMode = mode;
    sendToChildFrame(widget.frame, SHELL_WIDGET_MODE, { mode });
  }

  private takeSize(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const app = this.appOfFrame(frame);
    const { width, height } = payload;
    if (app === null || typeof width !== "number" || typeof height !== "number") return;
    if (!Number.isFinite(width) || !Number.isFinite(height)) return;
    const sentMode = this.frames.get(app)?.sentMode;
    this.store.reportWidgetSize(app, { width, height }, sentMode === "tray" || sentMode === "toolbar");
  }

  /** A click on a taskbar widget's page: its popover opens, or closes when open. */
  private takePress(frame: HTMLIFrameElement): void {
    const app = this.appOfFrame(frame);
    if (app === null || this.frames.get(app)?.host !== this.trayHost) return;
    hideTooltipAtRect(tooltipSource(app));
    this.store.toggleTrayPopover(app);
  }

  /** Escape on a widget's page: its popover closes, if it is the open one. */
  private takeDismiss(frame: HTMLIFrameElement): void {
    const app = this.appOfFrame(frame);
    if (app !== null && this.store.getState().trayPopoverApp === app) this.store.closeTrayPopover();
  }

  /** A right-click on a widget's page: its menu, at the point in the shell's viewport. */
  private takeMenu(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const app = this.appOfFrame(frame);
    const x = finiteNumber(payload.x);
    const y = finiteNumber(payload.y);
    if (app === null || x === null || y === null) return;
    hideTooltipAtRect(tooltipSource(app));
    const box = frame.getBoundingClientRect();
    this.options.onMenu(app, { x: box.left + x, y: box.top + y }, frame);
  }

  private takeTooltip(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const app = this.appOfFrame(frame);
    if (app === null) return;
    const { text } = payload;
    const left = finiteNumber(payload.left);
    const top = finiteNumber(payload.top);
    const width = finiteNumber(payload.width);
    const height = finiteNumber(payload.height);
    if (
      typeof text !== "string" ||
      text === "" ||
      left === null ||
      top === null ||
      width === null ||
      height === null
    ) {
      hideTooltipAtRect(tooltipSource(app));
      return;
    }
    const box = frame.getBoundingClientRect();
    const anchor = { left: box.left + left, top: box.top + top, bottom: box.top + top + height, width };
    showTooltipAtRect(tooltipSource(app), anchor, text);
  }

  private takeDrag(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const app = this.appOfFrame(frame);
    if (app === null) return;
    if (payload.phase === "start") hideTooltipAtRect(tooltipSource(app));
    if (payload.phase === "cancel") {
      if (this.dragOffsets.delete(app)) this.options.onDrag(app, { phase: "cancel" });
      return;
    }
    const screenX = finiteNumber(payload.screenX);
    const screenY = finiteNumber(payload.screenY);
    if (screenX === null || screenY === null) return;
    if (payload.phase === "start") {
      const pressX = finiteNumber(payload.pressX);
      const pressY = finiteNumber(payload.pressY);
      const pressScreenX = finiteNumber(payload.pressScreenX);
      const pressScreenY = finiteNumber(payload.pressScreenY);
      if (pressX === null || pressY === null || pressScreenX === null || pressScreenY === null) return;
      const box = frame.getBoundingClientRect();
      const press = { x: box.left + pressX, y: box.top + pressY };
      const offset = { x: press.x - pressScreenX, y: press.y - pressScreenY };
      this.dragOffsets.set(app, offset);
      this.options.onDrag(app, { phase: "start", point: { x: screenX + offset.x, y: screenY + offset.y }, press });
      return;
    }
    const offset = this.dragOffsets.get(app);
    if (offset === undefined || (payload.phase !== "move" && payload.phase !== "end")) return;
    if (payload.phase === "end") this.dragOffsets.delete(app);
    this.options.onDrag(app, { phase: payload.phase, point: { x: screenX + offset.x, y: screenY + offset.y } });
  }

  private takeHidden(frame: HTMLIFrameElement, payload: Record<string, unknown>): void {
    const app = this.appOfFrame(frame);
    if (app === null || typeof payload.isHidden !== "boolean") return;
    this.store.reportWidgetHidden(app, payload.isHidden);
  }
}
