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
  it("draws the standard slots, the icon and title centred, and marks every part a theme styles", () => {
    const bar = render();

    expect(bar.getAttribute("data-focused")).toBe("true");
    expect(slotOrder(bar)).toEqual(["menu", "refresh", "minimize", "maximize", "close"]);
    expect(bar.querySelector('[data-control="close"]')?.getAttribute("data-part")).toBe("window-control");
    // The icon and title sit together in the centred group, laid over the middle of the bar.
    const title = bar.querySelector('[data-part="window-title"]') as HTMLElement;
    expect(title.textContent).toBe("Plan");
    expect(title.parentElement?.className).toContain("absolute");
    expect(title.parentElement?.querySelector('[data-part="window-icon"]')).not.toBeNull();
  });

  it("offers Chat about this app after Refresh when the window's app takes one", () => {
    const bar = render({ hasAppChat: true });

    expect(slotOrder(bar)).toEqual(["menu", "refresh", "app-chat", "minimize", "maximize", "close"]);
    expect(bar.querySelector('[data-window-control="app-chat"]')?.getAttribute("aria-label")).toBe(
      "Chat about this app",
    );
    expect(slotOrder(render({ hasAppChat: false }))).toEqual(["menu", "refresh", "minimize", "maximize", "close"]);
  });

  it("arranges the controls as the theme's chrome says and centres the title over the bar", () => {
    const bar = render({ chrome: MAC_CHROME });

    expect(slotOrder(bar)).toEqual(["close", "refresh", "menu", "minimize", "maximize"]);
    // The chrome names no icon slot, so the centred group holds the title alone.
    const title = bar.querySelector('[data-part="window-title"]') as HTMLElement;
    expect(title.parentElement?.className).toContain("absolute");
    expect(title.parentElement?.querySelector('[data-part="window-icon"]')).toBeNull();
  });

  it("drops the window's own actions from a background window without leaving a gap, and draws restore when maximized", () => {
    const bar = render({ chrome: MAC_CHROME, isFocused: false, state: "MAXIMIZED" });

    expect(bar.getAttribute("data-focused")).toBe("false");
    expect(slotOrder(bar)).toEqual(["close", "minimize", "restore"]);
  });

  it("gives the first slot drawn in a run no leading margin when the slots before it are not drawn", () => {
    const chrome: ThemeChrome = {
      title_align: "center",
      leading: ["title", "refresh", "maximize", "close"],
      trailing: ["minimize"],
    };

    const background = render({ chrome, isFocused: false });
    const focused = render({ chrome });

    expect(background.querySelector('[data-control="maximize"]')?.className).not.toContain("ml-1");
    expect(focused.querySelector('[data-control="refresh"]')?.className).not.toContain("ml-2");
    expect(focused.querySelector('[data-control="maximize"]')?.className).toContain("ml-1");
  });

  it("keeps each control's element when the window it is pressed on takes focus", () => {
    let isFocused = false;
    const attrs: Omit<TitleBarAttrs, "isFocused"> = {
      title: "Plan",
      app: appRecord("docs"),
      state: "NORMAL",
      isMenuOpen: false,
      sizeMenuTrigger: {},
      onControl: vi.fn(),
      onDoubleClick: vi.fn(),
      chrome: STANDARD_CHROME,
    };
    const root = mountView(() => m(TitleBar, { ...attrs, isFocused }));
    const controlElements = (): (Element | null)[] =>
      ["minimize", "maximize", "close"].map((name) => root.querySelector(`[data-control="${name}"]`));
    const before = controlElements();

    isFocused = true;
    m.redraw.sync();

    expect(root.querySelector('[data-control="refresh"]')).not.toBeNull();
    controlElements().forEach((element, index) => expect(element).toBe(before[index]));
  });
});
