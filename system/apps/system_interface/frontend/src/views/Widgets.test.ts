// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ShownWidget } from "../reducers/desktopState";
import { appRecord } from "../testing/records";
import { Widgets } from "./Widgets";
import type { WidgetsAttrs } from "./Widgets";

afterEach(unmountViews);

const WIDGET = { path: "/widget", default_enabled: true, tray: false, toolbar: false };
const gauge: ShownWidget = {
  app: appRecord("gauge", { widget: WIDGET }),
  presentation: { position: { x: 1, y: 0.125 }, mode: "collapsed", placement: "desktop" },
};
const meter: ShownWidget = {
  app: appRecord("meter", { widget: WIDGET }),
  presentation: { position: null, mode: "expanded", placement: "desktop" },
};

function render(overrides: Partial<WidgetsAttrs> = {}): HTMLElement {
  const attrs: WidgetsAttrs = {
    widgets: [gauge, meter],
    rects: new Map([["gauge", { x: 808, y: 100, width: 192, height: 76 }]]),
    openMenuApp: null,
    onToggleMode: vi.fn(),
    onContextMenu: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(Widgets, attrs));
  return root.querySelector("[data-widgets]") as HTMLElement;
}

describe("Widgets", () => {
  it("draws each widget that has a box at it, with a content box to lay its page over, and no other", () => {
    const layer = render();
    expect(layer.classList.contains("pointer-events-none")).toBe(true);
    const widgets = [...layer.querySelectorAll("[data-widget]")];
    expect(widgets.map((widget) => widget.getAttribute("data-widget"))).toEqual(["gauge"]);
    const widget = widgets[0] as HTMLElement;
    expect([widget.style.left, widget.style.top, widget.style.width, widget.style.height]).toEqual([
      "808px",
      "100px",
      "192px",
      "76px",
    ]);
    expect(widget.getAttribute("data-widget-moved")).toBe("true");
    expect(widget.getAttribute("data-widget-mode")).toBe("collapsed");
    expect(widget.getAttribute("aria-label")).toContain("Gauge widget");
    expect(widget.querySelector("[data-widget-content]")).not.toBeNull();
    expect(widget.classList.contains("ring-2")).toBe(false);
  });

  it("marks a widget left in its default place, and the one whose menu is open", () => {
    const layer = render({
      rects: new Map([["meter", { x: 16, y: 16, width: 100, height: 50 }]]),
      openMenuApp: "meter",
    });
    const widget = layer.querySelector('[data-widget="meter"]') as HTMLElement;
    expect(widget.getAttribute("data-widget-moved")).toBe("false");
    expect(widget.classList.contains("ring-2")).toBe(true);
  });

  it("toggles a widget's mode on a double-click, and asks for its menu on a right-click where it was made", () => {
    const onToggleMode = vi.fn();
    const onContextMenu = vi.fn();
    const layer = render({ onToggleMode, onContextMenu });
    const widget = layer.querySelector('[data-widget="gauge"]') as HTMLElement;
    widget.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    expect(onToggleMode).toHaveBeenCalledWith("gauge");
    const click = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 820, clientY: 110 });
    widget.dispatchEvent(click);
    expect(click.defaultPrevented).toBe(true);
    expect(onContextMenu).toHaveBeenCalledWith("gauge", 820, 110, widget);
  });
});
