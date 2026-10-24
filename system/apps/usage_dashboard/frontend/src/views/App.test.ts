// @vitest-environment jsdom

import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DROPDOWN_OPTION_ATTR } from "@imbue/workspace-ui/src/components/dropdown";

import { DashboardModel } from "../models/dashboard";
import { DEFAULT_WIDGET_LOOK } from "../models/widgetLook";
import type { WidgetLook, WidgetLookPicker, WidgetPlace } from "../models/widgetLook";
import { App } from "./App";

const NOW = new Date("2026-10-22T20:00:00Z");

/** A picker that takes every pick at once, and records each. */
interface FakePicker {
  look: WidgetLook | null;
  errorMessage: string | null;
  select: ReturnType<typeof vi.fn<(change: Partial<WidgetLook>) => Promise<void>>>;
}

function fakePicker(look: WidgetLook | null, errorMessage: string | null = null): FakePicker {
  const picker: FakePicker = {
    look,
    errorMessage,
    select: vi.fn(async (change: Partial<WidgetLook>) => {
      picker.look = { ...(picker.look ?? DEFAULT_WIDGET_LOOK), ...change };
    }),
  };
  return picker;
}

let root: HTMLElement | null = null;

/** The Usage page's home, never started, mounted with ``picker`` at its foot. */
function mounted(picker: FakePicker): HTMLElement {
  const model = new DashboardModel({
    route: { page: "home" },
    now: () => NOW,
    redraw: () => undefined,
    openStream: () => {
      throw new Error("the page is never started here");
    },
  });
  root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, {
    view: () =>
      m(App, { model, lookPicker: picker as unknown as WidgetLookPicker, route: { page: "home" }, now: () => NOW }),
  });
  return root;
}

afterEach(() => {
  if (root !== null) m.mount(root, null);
  root = null;
  document.body.innerHTML = "";
});

function footer(): HTMLElement | null {
  return document.querySelector("footer.widget-style");
}

/** Each menu in the picker, by its label, with the choice it shows. */
function menus(): Record<string, string | null> {
  const triggers = [...(footer()?.querySelectorAll('[role="combobox"]') ?? [])];
  return Object.fromEntries(
    triggers.map((trigger) => [trigger.getAttribute("aria-label"), trigger.textContent?.trim() ?? null]),
  );
}

function click(element: Element | null | undefined): void {
  if (element === null || element === undefined) throw new Error("nothing to click");
  element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  m.redraw.sync();
}

/** Open the menu labelled ``label`` and pick ``value`` from it. */
function pick(label: string, value: string): void {
  click(footer()?.querySelector(`[aria-label="${label}"]`));
  click(document.querySelector(`[${DROPDOWN_OPTION_ATTR}="${value}"]`));
}

describe("the widget picker at the page's foot", () => {
  const MENUS_BY_PLACE: Record<WidgetPlace, Record<string, string>> = {
    desktop: {
      "Widget style": "Bars",
      "Widget size": "Large",
      "How much the widget shows": "More",
    },
    toolbar: {
      "Widget design in the toolbar": "Meters",
      "How much the widget shows": "More",
    },
    tray: {
      "Widget design in the tray": "Stacked",
      "How much the widget shows": "More",
    },
  };

  for (const place of ["desktop", "toolbar", "tray"] as const) {
    it(`offers the place first, then only the ${place}'s own choices, each showing the stored one`, () => {
      const look: WidgetLook = {
        variant: "bars",
        size: "large",
        detail: "more",
        tray: "stacked",
        place,
        toolbar: "meters",
      };
      mounted(fakePicker(look));

      const places = footer()?.querySelector('[role="radiogroup"][aria-label="Where the widget goes"]');
      const segments = [...(places?.querySelectorAll("[data-widget-place]") ?? [])];
      expect(segments.map((segment) => [segment.textContent, segment.getAttribute("aria-checked")])).toEqual([
        ["Desktop", place === "desktop" ? "true" : "false"],
        ["Toolbar", place === "toolbar" ? "true" : "false"],
        ["Tray", place === "tray" ? "true" : "false"],
      ]);
      expect(menus()).toEqual(MENUS_BY_PLACE[place]);
      // The place comes before its choices.
      const order = [...(footer()?.querySelectorAll('[role="radiogroup"], [role="combobox"]') ?? [])];
      expect(order[0]).toBe(places);
    });
  }

  it("saves each pick through the picker, and shows the picked place's choices at once", () => {
    const picker = fakePicker(DEFAULT_WIDGET_LOOK);
    mounted(picker);

    pick("Widget style", "minimal");
    pick("Widget size", "small");
    pick("How much the widget shows", "less");
    expect(picker.select.mock.calls.map(([change]) => change)).toEqual([
      { variant: "minimal" },
      { size: "small" },
      { detail: "less" },
    ]);
    expect(menus()).toEqual({
      "Widget style": "Rings · Minimal",
      "Widget size": "Small",
      "How much the widget shows": "Less",
    });

    click(footer()?.querySelector('[data-widget-place="toolbar"]'));
    expect(picker.select).toHaveBeenLastCalledWith({ place: "toolbar" });
    expect(Object.keys(menus())).toEqual(["Widget design in the toolbar", "How much the widget shows"]);
    pick("Widget design in the toolbar", "inline");
    expect(picker.select).toHaveBeenLastCalledWith({ toolbar: "inline" });

    click(footer()?.querySelector('[data-widget-place="tray"]'));
    expect(picker.select).toHaveBeenLastCalledWith({ place: "tray" });
    pick("Widget design in the tray", "numbers");
    expect(picker.select).toHaveBeenLastCalledWith({ tray: "numbers" });
    expect(menus()).toEqual({ "Widget design in the tray": "Numbers", "How much the widget shows": "Less" });
  });

  it("does not offer the place already picked again", () => {
    const picker = fakePicker(DEFAULT_WIDGET_LOOK);
    mounted(picker);
    click(footer()?.querySelector('[data-widget-place="desktop"]'));
    expect(picker.select).not.toHaveBeenCalled();
  });

  it("shows a failed pick's error beside the choices", () => {
    mounted(fakePicker(DEFAULT_WIDGET_LOOK, "Could not change the widget: boom"));
    expect(footer()?.querySelector('[role="alert"]')?.textContent).toBe("Could not change the widget: boom");
    expect(Object.keys(menus())).toHaveLength(3);
  });

  it("offers nothing to pick while the look is unread: just the error when the read failed, else nothing", () => {
    mounted(fakePicker(null, "Could not read the widget's settings"));
    expect(footer()?.textContent).toBe("Could not read the widget's settings");
    expect(footer()?.querySelector('[role="alert"]')).not.toBeNull();
    expect(footer()?.querySelector('[role="radiogroup"], [role="combobox"]')).toBeNull();
    m.mount(root as HTMLElement, null);
    document.body.innerHTML = "";

    mounted(fakePicker(null));
    expect(footer()).toBeNull();
  });
});
