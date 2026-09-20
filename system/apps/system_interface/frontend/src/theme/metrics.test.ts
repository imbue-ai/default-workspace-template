import { readFileSync } from "fs";
import { describe, expect, it, vi } from "vitest";
import type { MediaQueryLike } from "./metrics";
import {
  COMPACT_ATTRIBUTE,
  COMPACT_MAX_WIDTH_PX,
  COMPACT_MEDIA_QUERY,
  TOUCH_ATTRIBUTE,
  TOUCH_MEDIA_QUERY,
  ThemeMetricsError,
  applyRenderModes,
  followRenderModes,
  parsePixelLength,
  readThemeMetrics,
} from "./metrics";

const THEME_CSS = readFileSync(new URL("./default.css", import.meta.url).pathname, "utf-8");

/** The value a token has in the `:root` block of the theme file. */
function rootToken(token: string): string {
  const root = THEME_CSS.slice(THEME_CSS.indexOf(":root {"), THEME_CSS.indexOf("[data-compact]"));
  const match = new RegExp(`${token}:\\s*([^;]+);`).exec(root);
  if (match === null) throw new Error(`no ${token} on :root`);
  return match[1].trim();
}

/** A computed style over a token table. */
function styleOf(tokens: Record<string, string>): { getPropertyValue: (name: string) => string } {
  return { getPropertyValue: (name: string) => tokens[name] ?? "" };
}

const CONTRACT_TOKENS: Record<string, string> = {
  "--desk-title-bar-height": "36px",
  "--desk-taskbar-height": "48px",
  "--desk-cell-width": "96px",
  "--desk-cell-height": "112px",
  "--desk-grid-inset": "16px",
  "--desk-window-min-width": "320px",
  "--desk-window-min-height": "240px",
  "--desk-title-min-visible": "120px",
  "--desk-snap-threshold": "16px",
  "--desk-unsnap-distance": "12px",
  "--desk-drag-threshold": "4px",
  "--desk-touch-target": "32px",
};

describe("the theme file", () => {
  it("declares the contract's default metrics on :root", () => {
    for (const [token, value] of Object.entries(CONTRACT_TOKENS)) expect(rootToken(token), token).toBe(value);
    expect(rootToken("--desk-default-wallpaper")).toMatch(/^url\(\/wallpapers\/bundled\/[a-z0-9-]+\)$/);
  });

  it("redeclares the compact and touch values under their attributes", () => {
    const compact = THEME_CSS.slice(THEME_CSS.indexOf("[data-compact]"), THEME_CSS.indexOf("[data-touch]"));
    expect(compact).toContain("--desk-taskbar-height: 56px");
    expect(compact).toContain("--desk-cell-width: 80px");
    expect(compact).toContain("--desk-cell-height: 96px");
    expect(compact).toContain("--desk-grid-inset: 8px");
    expect(compact).toContain("--desk-window-radius: 0px");
    const touch = THEME_CSS.slice(THEME_CSS.indexOf("[data-touch]"));
    expect(touch).toContain("--desk-title-bar-height: 44px");
    expect(touch).toContain("--desk-taskbar-height: 56px");
    expect(touch).toContain("--desk-drag-threshold: 8px");
    expect(touch).toContain("--desk-touch-target: 44px");
  });

  it("applies the compact breakpoint as a media query", () => {
    expect(COMPACT_MEDIA_QUERY).toBe(`(max-width: ${COMPACT_MAX_WIDTH_PX}px)`);
    expect(TOUCH_MEDIA_QUERY).toBe("(pointer: coarse)");
  });
});

describe("readThemeMetrics", () => {
  it("reads every metric off the computed tokens", () => {
    expect(readThemeMetrics(styleOf(CONTRACT_TOKENS))).toEqual({
      titleBarHeight: 36,
      taskbarHeight: 48,
      cellWidth: 96,
      cellHeight: 112,
      gridInset: 16,
      windowMinWidth: 320,
      windowMinHeight: 240,
      titleMinVisible: 120,
      snapThreshold: 16,
      unsnapDistance: 12,
      dragThreshold: 4,
      touchTarget: 32,
    });
  });

  it("refuses a missing or non-pixel token", () => {
    expect(() => readThemeMetrics(styleOf({ ...CONTRACT_TOKENS, "--desk-cell-width": "" }))).toThrow(
      ThemeMetricsError,
    );
    expect(() => parsePixelLength("--x", "2rem")).toThrow(ThemeMetricsError);
    expect(parsePixelLength("--x", " 12.5px ")).toBe(12.5);
  });
});

/** A media query a test flips by hand. */
function fakeQuery(matches: boolean): MediaQueryLike & { flip: (matches: boolean) => void } {
  const listeners = new Set<() => void>();
  const query = {
    matches,
    addEventListener: (_type: "change", listener: () => void) => void listeners.add(listener),
    removeEventListener: (_type: "change", listener: () => void) => void listeners.delete(listener),
    flip: (next: boolean) => {
      query.matches = next;
      for (const listener of listeners) listener();
    },
  };
  return query;
}

/** A root element stub: the attributes stamped on it, and a style whose tokens follow them. */
function fakeRoot(): { element: HTMLElement; style: () => { getPropertyValue: (name: string) => string } } {
  const attributes = new Map<string, string>();
  const element = {
    setAttribute: (name: string, value: string) => void attributes.set(name, value),
    removeAttribute: (name: string) => void attributes.delete(name),
    hasAttribute: (name: string) => attributes.has(name),
  } as unknown as HTMLElement;
  return {
    element,
    style: () =>
      styleOf({
        ...CONTRACT_TOKENS,
        "--desk-taskbar-height": attributes.has(COMPACT_ATTRIBUTE) ? "56px" : "48px",
        "--desk-touch-target": attributes.has(TOUCH_ATTRIBUTE) ? "44px" : "32px",
      }),
  };
}

describe("followRenderModes", () => {
  it("stamps the attributes and re-reads the metrics on every change", () => {
    const compact = fakeQuery(false);
    const touch = fakeQuery(true);
    const root = fakeRoot();
    const onChange = vi.fn();
    const stop = followRenderModes(
      root.element,
      (query) => (query === COMPACT_MEDIA_QUERY ? compact : touch),
      root.style,
      onChange,
    );
    expect(root.element.hasAttribute(COMPACT_ATTRIBUTE)).toBe(false);
    expect(root.element.hasAttribute(TOUCH_ATTRIBUTE)).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith(
      { isCompact: false, isTouch: true },
      expect.objectContaining({ taskbarHeight: 48, touchTarget: 44 }),
    );

    compact.flip(true);
    expect(root.element.hasAttribute(COMPACT_ATTRIBUTE)).toBe(true);
    expect(onChange).toHaveBeenLastCalledWith(
      { isCompact: true, isTouch: true },
      expect.objectContaining({ taskbarHeight: 56 }),
    );

    stop();
    compact.flip(false);
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("applyRenderModes removes what no longer matches", () => {
    const root = fakeRoot();
    applyRenderModes(root.element, { isCompact: true, isTouch: true });
    applyRenderModes(root.element, { isCompact: false, isTouch: false });
    expect(root.element.hasAttribute(COMPACT_ATTRIBUTE)).toBe(false);
    expect(root.element.hasAttribute(TOUCH_ATTRIBUTE)).toBe(false);
  });
});
