// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ThemeIcons } from "../model/themes";
import { themeRecord } from "../testing/records";
import { appGlyph } from "./glyphs";
import { resetThemeIconsForTests, setIconTheme, themedIconFor, transformPixels } from "./themeIcons";

afterEach(() => {
  resetThemeIconsForTests();
  vi.restoreAllMocks();
});

const STANDARD_MARKUP = '<svg viewBox="0 0 216 216"><rect width="216" height="216" fill="#2f6b4f"/></svg>';

function icons(overrides: Partial<ThemeIcons>): ThemeIcons {
  return {
    format: "png",
    size: 32,
    rendering: "pixelated",
    background: "transparent",
    palette: [],
    max_colors: null,
    derive: "none",
    fallback_url: "/api/themes/paper/icons/app.png",
    apps: { files: "/api/themes/paper/icons/files.png" },
    ...overrides,
  };
}

describe("themedIconFor", () => {
  it("draws the standard icon under the standard theme", () => {
    setIconTheme(themeRecord("standard"));
    expect(themedIconFor("files", () => STANDARD_MARKUP)).toBeNull();
  });

  it("draws the theme's own icon for an app it has one for, and its generic program icon otherwise", () => {
    setIconTheme(themeRecord("paper", { icons: icons({}) }));

    expect(themedIconFor("files", () => STANDARD_MARKUP)).toEqual({
      url: "/api/themes/paper/icons/files.png",
      source: "theme",
      isPixelated: true,
    });
    expect(themedIconFor("notes", () => STANDARD_MARKUP)).toEqual({
      url: "/api/themes/paper/icons/app.png",
      source: "fallback",
      isPixelated: true,
    });
  });

  it("shows the fallback while an icon is derived from the standard one, which needs a canvas", () => {
    setIconTheme(themeRecord("paper", { icons: icons({ derive: "monochrome" }) }));

    // jsdom has no canvas, so the derivation never lands and the fallback keeps showing.
    expect(themedIconFor("notes", () => STANDARD_MARKUP)?.source).toBe("fallback");
    expect(themedIconFor("notes", () => STANDARD_MARKUP)?.source).toBe("fallback");
  });

  it("derives again when the app registers a new standard icon, and only then", () => {
    setIconTheme(themeRecord("paper", { icons: icons({ derive: "monochrome" }) }));
    const createElement = vi.spyOn(document, "createElement");
    const derivationsStarted = (): number =>
      createElement.mock.calls.filter(([tagName]) => tagName === "canvas").length;

    themedIconFor("notes", () => STANDARD_MARKUP);
    themedIconFor("notes", () => STANDARD_MARKUP);
    expect(derivationsStarted()).toBe(1);

    themedIconFor("notes", () => STANDARD_MARKUP.replace("#2f6b4f", "#7a3b2e"));
    expect(derivationsStarted()).toBe(2);
  });

  it("derives an app's icon once, whatever size each surface draws it at", () => {
    setIconTheme(themeRecord("paper", { icons: icons({ derive: "monochrome" }) }));
    const createElement = vi.spyOn(document, "createElement");
    const derivationsStarted = (): number =>
      createElement.mock.calls.filter(([tagName]) => tagName === "canvas").length;

    appGlyph({ name: "notes", icon: STANDARD_MARKUP }, 20);
    appGlyph({ name: "notes", icon: STANDARD_MARKUP }, 48);
    expect(derivationsStarted()).toBe(1);
  });

  it("forgets the icons derived under a theme once another is drawn", () => {
    const paper = themeRecord("paper", { icons: icons({ derive: "monochrome" }) });
    const ink = themeRecord("ink", { icons: icons({ derive: "monochrome" }) });
    const createElement = vi.spyOn(document, "createElement");
    const derivationsStarted = (): number =>
      createElement.mock.calls.filter(([tagName]) => tagName === "canvas").length;

    setIconTheme(paper);
    themedIconFor("notes", () => STANDARD_MARKUP);
    setIconTheme(paper);
    themedIconFor("notes", () => STANDARD_MARKUP);
    expect(derivationsStarted()).toBe(1);

    setIconTheme(ink);
    themedIconFor("notes", () => STANDARD_MARKUP);
    setIconTheme(paper);
    themedIconFor("notes", () => STANDARD_MARKUP);
    expect(derivationsStarted()).toBe(3);
  });

  it("marks the glyph the desktop draws with the part and where its icon came from", () => {
    setIconTheme(themeRecord("paper", { icons: icons({}) }));
    const themed = appGlyph({ name: "files", icon: "" }, 20);
    expect(themed).toContain('data-part="app-icon"');
    expect(themed).toContain('data-icon-source="theme"');
    expect(themed).toContain("image-rendering: pixelated");

    setIconTheme(themeRecord("standard"));
    expect(appGlyph({ name: "files", icon: "" }, 20)).toContain('data-icon-source="standard"');
  });

  it("keeps an icon's URL inside its attribute", () => {
    setIconTheme(themeRecord("paper", { icons: icons({ apps: { files: '/icon.png?a=1&b="x"><script>' } }) }));
    const host = document.createElement("div");
    host.innerHTML = appGlyph({ name: "files", icon: "" }, 20);

    expect(host.querySelector("script")).toBeNull();
    expect(host.querySelector("image")?.getAttribute("href")).toBe('/icon.png?a=1&b="x"><script>');
  });
});

describe("transformPixels", () => {
  function pixels(...rgba: number[][]): Uint8ClampedArray {
    return new Uint8ClampedArray(rgba.flat());
  }

  it("turns every opaque pixel black or white by brightness for a monochrome theme, and alpha to all or none", () => {
    const data = pixels([30, 40, 50, 255], [220, 210, 200, 200], [100, 100, 100, 60]);
    transformPixels(data, icons({ derive: "monochrome" }));
    expect(Array.from(data)).toEqual([0, 0, 0, 255, 255, 255, 255, 255, 100, 100, 100, 0]);
  });

  it("makes alpha all or none for a pixelate derivation under a smooth theme, and leaves the colors", () => {
    const data = pixels([30, 40, 50, 200], [220, 210, 200, 60]);
    transformPixels(data, icons({ derive: "pixelate", rendering: "smooth" }));
    expect(Array.from(data)).toEqual([30, 40, 50, 255, 220, 210, 200, 0]);
  });

  it("maps to the palette, or to evenly spaced levels when there is none", () => {
    const toPalette = pixels([250, 10, 10, 255]);
    transformPixels(toPalette, icons({ derive: "quantize", palette: ["#000000", "#ff0000", "#ffffff"] }));
    expect(Array.from(toPalette)).toEqual([255, 0, 0, 255]);

    const toLevels = pixels([120, 10, 250, 255]);
    transformPixels(toLevels, icons({ derive: "quantize", max_colors: 8, rendering: "smooth" }));
    expect(Array.from(toLevels)).toEqual([0, 0, 255, 255]);
  });
});
