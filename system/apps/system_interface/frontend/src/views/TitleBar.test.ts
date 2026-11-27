// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { STANDARD_CHROME, type ThemeChrome } from "../model/themes";
import { appRecord } from "../testing/records";
import { shareLookOf, TitleBar, type ShareFitting, type TitleBarAttrs } from "./TitleBar";

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

/** A theme that sets the title in the bar's own run, after the icon, with the window's actions at the far end. */
const START_CHROME: ThemeChrome = {
  title_align: "start",
  leading: ["icon", "title"],
  trailing: ["refresh", "menu", "minimize", "maximize", "close"],
};

const SHORT_TITLE = "Chat";
const LONG_TITLE = "Quarterly planning for the team";
const VERY_LONG_TITLE = "Quarterly planning notes for the whole team, with the budget and the hiring plan";

/** Share offered on a bar of this width, on a desktop client. */
function shareOn(barWidth: number): TitleBarAttrs["share"] {
  return { barWidth, isTouch: false };
}

function shareOf(bar: HTMLElement): HTMLElement {
  const share = bar.querySelector<HTMLElement>('[data-window-control="share"]');
  if (share === null) throw new Error("no Share control");
  return share;
}

/** How a rendered bar draws Share, read off the page. */
function shareLookIn(bar: HTMLElement): "labelled" | "glyph" | "hidden" {
  const share = bar.querySelector('[data-window-control="share"]');
  if (share === null) return "hidden";
  return share.textContent === "Share" ? "labelled" : "glyph";
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

  it("offers Share after the balloon, or after Refresh on a bar with no balloon, as a control a theme can style", () => {
    const bar = render({ hasAppChat: true, share: shareOn(600) });

    expect(slotOrder(bar)).toEqual(["menu", "refresh", "app-chat", "share", "minimize", "maximize", "close"]);
    const share = shareOf(bar);
    expect(share.getAttribute("data-part")).toBe("window-control");
    expect(share.getAttribute("data-control")).toBe("share");
    expect(share.getAttribute("aria-label")).toBe("Share Docs");
    expect(share.querySelector("svg")).not.toBeNull();
    expect(slotOrder(render({ share: shareOn(600) }))).toEqual([
      "menu",
      "refresh",
      "share",
      "minimize",
      "maximize",
      "close",
    ]);
  });

  it("offers no Share where the caller offers none, and none on a background window", () => {
    expect(slotOrder(render({ hasAppChat: true }))).toEqual([
      "menu",
      "refresh",
      "app-chat",
      "minimize",
      "maximize",
      "close",
    ]);
    expect(slotOrder(render({ hasAppChat: true, share: shareOn(600), isFocused: false }))).toEqual([
      "minimize",
      "maximize",
      "close",
    ]);
  });

  it("keeps Share beside Refresh wherever the theme's chrome puts that slot", () => {
    const bar = render({ chrome: MAC_CHROME, hasAppChat: true, share: shareOn(600) });

    expect(slotOrder(bar)).toEqual([
      "close",
      "refresh",
      "app-chat",
      "share",
      "menu",
      "minimize",
      "maximize",
      "window-title",
    ]);
  });

  it("reports a click on Share as the share control, and not as a double click on the bar", () => {
    const onControl = vi.fn();
    const onDoubleClick = vi.fn();
    const share = shareOf(render({ share: shareOn(600), onControl, onDoubleClick }));

    share.click();
    share.dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));

    expect(onControl).toHaveBeenCalledTimes(1);
    expect(onControl).toHaveBeenCalledWith("share", expect.anything());
    expect(onDoubleClick).not.toHaveBeenCalled();
  });

  it("writes Share's word beside its glyph where a short title leaves room for both", () => {
    const share = shareOf(render({ title: SHORT_TITLE, hasAppChat: true, share: shareOn(600) }));

    expect(share.textContent).toBe("Share");
    expect(share.querySelector("svg")).not.toBeNull();
    expect(share.getAttribute("data-shape")).toBe("text");
  });

  it("draws Share's glyph alone where a long title in the same window leaves no room for the word", () => {
    const bar = render({ title: LONG_TITLE, hasAppChat: true, share: shareOn(600) });
    const share = shareOf(bar);

    expect(share.textContent).toBe("");
    expect(share.querySelector("svg")).not.toBeNull();
    // Without the word it is the same square box as the controls beside it, and still says what it does.
    expect(share.getAttribute("data-shape")).toBe("icon");
    expect(share.className).toBe((bar.querySelector('[data-window-control="app-chat"]') as HTMLElement).className);
    expect(share.getAttribute("aria-label")).toBe("Share Docs");
  });

  it("draws no Share where a very long title in a narrow window would lie under even its glyph", () => {
    const bar = render({ title: VERY_LONG_TITLE, hasAppChat: true, share: shareOn(400) });

    expect(slotOrder(bar)).toEqual(["menu", "refresh", "app-chat", "minimize", "maximize", "close"]);
    // The same title leaves the glyph room on a wider bar, and the word on a wider one still.
    expect(shareLookIn(render({ title: VERY_LONG_TITLE, share: shareOn(700) }))).toBe("glyph");
    expect(shareLookIn(render({ title: VERY_LONG_TITLE, share: shareOn(1000) }))).toBe("labelled");
  });

  it("asks more room of a touch client, whose controls are wider", () => {
    const looks = (isTouch: boolean): string[] =>
      [440, 480, 560].map((barWidth) => shareLookIn(render({ title: SHORT_TITLE, share: { barWidth, isTouch } })));

    expect(looks(false)).toEqual(["labelled", "labelled", "labelled"]);
    expect(looks(true)).toEqual(["hidden", "glyph", "labelled"]);
  });

  it("always writes Share's word where the theme sets the title in the bar's own run, which nothing can sit on", () => {
    const bar = render({ chrome: START_CHROME, title: VERY_LONG_TITLE, share: shareOn(320) });

    expect(slotOrder(bar)).toEqual([
      "window-icon",
      "window-title",
      "refresh",
      "share",
      "menu",
      "minimize",
      "maximize",
      "close",
    ]);
    expect(shareLookIn(bar)).toBe("labelled");
    expect(
      shareLookIn(render({ chrome: START_CHROME, title: VERY_LONG_TITLE, share: { barWidth: 320, isTouch: true } })),
    ).toBe("labelled");
  });

  it("arranges the controls as the theme's chrome says and centres the title over the bar", () => {
    const bar = render({ chrome: MAC_CHROME });

    expect(slotOrder(bar)).toEqual(["close", "refresh", "menu", "minimize", "maximize", "window-title"]);
    // The chrome names no icon slot, so the title part itself is the centred element the theme styles.
    const title = bar.querySelector('[data-part="window-title"]') as HTMLElement;
    expect(title.className).toContain("absolute");
    expect(bar.querySelector('[data-part="window-icon"]')).toBeNull();
  });

  it("drops the window's own actions from a background window without leaving a gap, and draws fullscreen and restore when maximized", () => {
    const bar = render({ chrome: MAC_CHROME, isFocused: false, state: "MAXIMIZED" });

    expect(bar.getAttribute("data-focused")).toBe("false");
    expect(slotOrder(bar)).toEqual(["close", "minimize", "fullscreen", "restore", "window-title"]);
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

describe("shareLookOf", () => {
  const fitting = (overrides: Partial<ShareFitting>): ShareFitting => ({
    barWidth: 600,
    title: SHORT_TITLE,
    isTitleCentered: true,
    hasCenteredIcon: true,
    isTouch: false,
    ...overrides,
  });
  const looksAt = (widths: readonly number[], overrides: Partial<ShareFitting>): string[] =>
    widths.map((barWidth) => shareLookOf(fitting({ ...overrides, barWidth })));

  it("draws the fullest look the room before the centred title holds, at the width each stops fitting", () => {
    // "Chat" with its icon is taken to be 58 wide: the word needs a bar of 408, the glyph one of 322.
    expect(looksAt([321, 322, 407, 408], { title: SHORT_TITLE })).toEqual(["hidden", "glyph", "glyph", "labelled"]);
    expect(looksAt([600], { title: "Getting Started" })).toEqual(["labelled"]);
    expect(looksAt([600], { title: LONG_TITLE })).toEqual(["glyph"]);
  });

  it("takes a title no wider than the three fifths of the bar its element is capped at", () => {
    const endless = "x".repeat(400);

    expect(looksAt([659, 660, 874, 875], { title: endless })).toEqual(["hidden", "glyph", "glyph", "labelled"]);
  });

  it("counts the app's icon only where the theme centres it with the title", () => {
    expect(looksAt([400], { title: SHORT_TITLE, hasCenteredIcon: true })).toEqual(["glyph"]);
    expect(looksAt([400], { title: SHORT_TITLE, hasCenteredIcon: false })).toEqual(["labelled"]);
  });

  it("asks a touch client's longer run for more room", () => {
    expect(looksAt([449, 450, 503, 504], { title: SHORT_TITLE, isTouch: true })).toEqual([
      "hidden",
      "glyph",
      "glyph",
      "labelled",
    ]);
  });

  it("writes the word whatever the title and the width where the title is not centred", () => {
    expect(looksAt([320, 600], { title: "x".repeat(400), isTitleCentered: false, isTouch: true })).toEqual([
      "labelled",
      "labelled",
    ]);
  });
});
