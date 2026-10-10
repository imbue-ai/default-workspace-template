// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STANDARD_CHROME, type ThemeChrome } from "../model/themes";
import { appRecord } from "../testing/records";
import { TitleBar, type TitleBarAttrs } from "./TitleBar";

afterEach(unmountViews);

const MAC_CHROME: ThemeChrome = {
  title_align: "center",
  leading: ["close", "refresh", "menu", "title"],
  trailing: ["minimize", "maximize"],
};

function render(overrides: Partial<TitleBarAttrs> = {}): HTMLElement {
  const attrs: TitleBarAttrs = {
    title: "Plan",
    app: appRecord("docs"),
    state: "NORMAL",
    isFocused: true,
    isMenuOpen: false,
    sizeMenuTrigger: {},
    onControl: vi.fn(),
    onDoubleClick: vi.fn(),
    chrome: STANDARD_CHROME,
    ...overrides,
  };
  const root = mountView(() => m(TitleBar, attrs));
  return root.querySelector('[data-part="title-bar"]') as HTMLElement;
}

/** The bar's children in order, each named by its part's control or part name. */
function slotOrder(bar: HTMLElement): string[] {
  return Array.from(bar.children)
    .map((child) => child.getAttribute("data-control") ?? child.getAttribute("data-part") ?? "spacer")
    .filter((name) => name !== "spacer");
}

describe("TitleBar", () => {
  it("draws the standard slots and marks every part a theme styles", () => {
    const bar = render();

    expect(bar.getAttribute("data-focused")).toBe("true");
    expect(slotOrder(bar)).toEqual([
      "window-icon",
      "window-title",
      "refresh",
      "menu",
      "minimize",
      "maximize",
      "close",
    ]);
    expect(bar.querySelector('[data-control="close"]')?.getAttribute("data-part")).toBe("window-control");
    expect(bar.querySelector('[data-part="window-title"]')?.textContent).toBe("Plan");
  });

  it("arranges the controls as the theme's chrome says and centres the title over the bar", () => {
    const bar = render({ chrome: MAC_CHROME });

    expect(slotOrder(bar)).toEqual(["close", "refresh", "menu", "minimize", "maximize", "window-title"]);
    expect(bar.querySelector('[data-part="window-icon"]')).toBeNull();
    expect(bar.querySelector('[data-part="window-title"]')?.className).toContain("absolute");
  });

  it("drops the window's own actions from a background window without leaving a gap, and draws restore when maximized", () => {
    const bar = render({ chrome: MAC_CHROME, isFocused: false, state: "MAXIMIZED" });

    expect(bar.getAttribute("data-focused")).toBe("false");
    expect(slotOrder(bar)).toEqual(["close", "minimize", "restore", "window-title"]);
  });
});
