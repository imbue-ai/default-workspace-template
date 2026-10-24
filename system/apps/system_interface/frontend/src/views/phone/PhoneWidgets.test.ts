// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { ShownWidget, WidgetReport } from "../../reducers/desktopState";
import { appRecord, themeMetricsRecord } from "../../testing/records";
import { PhoneWidgets } from "./PhoneWidgets";

afterEach(unmountViews);

const METRICS = themeMetricsRecord();

function shown(name: string): ShownWidget {
  return {
    app: appRecord(name, { widget: { path: "/widget", default_enabled: true, tray: false, toolbar: false } }),
    presentation: { position: null, mode: "expanded", placement: "desktop" },
  };
}

function render(reports: Record<string, WidgetReport>): HTMLElement {
  const widgets = ["gauge", "meter", "dial", "clock"].map(shown);
  return mountView(() => m(PhoneWidgets, { widgets, reports, metrics: METRICS }));
}

describe("PhoneWidgets", () => {
  it("draws each widget whose page reported a size and is not hidden, at that size clamped to the theme's", () => {
    const root = render({
      gauge: { size: { width: 180, height: 60 }, traySize: null, isHidden: false },
      meter: { size: { width: 900, height: 4 }, traySize: null, isHidden: false },
      dial: { size: { width: 100, height: 40 }, traySize: null, isHidden: true },
      clock: { size: null, traySize: null, isHidden: false },
    });
    const strip = root.querySelector("[data-phone-widgets]") as HTMLElement;
    const widgets = [...strip.querySelectorAll<HTMLElement>("[data-phone-widget]")];
    expect(widgets.map((widget) => widget.getAttribute("data-widget"))).toEqual(["gauge", "meter"]);
    expect(widgets.map((widget) => [widget.style.width, widget.style.height])).toEqual([
      ["180px", "60px"],
      ["320px", "24px"],
    ]);
    expect(widgets[0].querySelector("[data-widget-content]")).not.toBeNull();
  });

  it("draws no strip while no widget has anything to show", () => {
    const root = render({ dial: { size: { width: 100, height: 40 }, traySize: null, isHidden: true } });
    expect(root.querySelector("[data-phone-widgets]")).toBeNull();
  });
});
