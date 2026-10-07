// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { ThemeChrome } from "../model/themes";
import { appRecord, themeCatalog, themeRecord } from "../testing/records";
import { ThemeGallery, galleryStateOf } from "./ThemeGallery";

afterEach(unmountViews);

const CENTERED_CHROME: ThemeChrome = {
  title_align: "center",
  leading: ["close", "refresh", "menu", "title"],
  trailing: ["minimize", "maximize"],
};

const BROKEN = themeRecord("broken", {
  name: "Broken",
  available: false,
  problems: ["parts.css:1: .x is a class"],
  chrome: CENTERED_CHROME,
});

describe("galleryStateOf", () => {
  it("shows an available theme in itself", () => {
    const paper = themeRecord("paper", { chrome: CENTERED_CHROME });
    const state = galleryStateOf(themeCatalog(paper), "paper", [appRecord("files")]);
    expect(state?.theme).toBe(paper);
    expect(state?.shown).toBe(paper);
  });

  it("shows an unavailable theme in the standard look, whose files are served", () => {
    const catalog = themeCatalog(BROKEN);
    const state = galleryStateOf(catalog, "broken", [appRecord("files")]);
    expect(state?.theme).toBe(BROKEN);
    expect(state?.shown.id).toBe("standard");
  });

  it("answers null for a theme the catalog does not have, and leaves internal apps out", () => {
    expect(galleryStateOf(themeCatalog(), "missing", [])).toBeNull();
    const state = galleryStateOf(themeCatalog(), "standard", [
      appRecord("files"),
      appRecord("shell", { internal: true }),
    ]);
    expect(state?.apps.map((app) => app.name)).toEqual(["files"]);
  });
});

describe("ThemeGallery", () => {
  it("heads an unavailable theme's page with its name and problems, and draws its windows in the standard chrome", () => {
    const state = galleryStateOf(themeCatalog(BROKEN), "broken", [appRecord("files")]);
    if (state === null) throw new Error("the catalog has the broken theme");
    const root = mountView(() => m(ThemeGallery, { state }));

    expect(root.querySelector("h1")?.textContent).toBe("Broken");
    expect(root.querySelector("[data-gallery-problems]")?.textContent).toContain(".x is a class");
    const bar = root.querySelector('[data-part="title-bar"]') as HTMLElement;
    const controls = Array.from(bar.querySelectorAll("[data-control]")).map((c) => c.getAttribute("data-control"));
    expect(controls).toEqual(["refresh", "menu", "minimize", "maximize", "close"]);
  });

  it("swatches the terminal palette a theme sets, and says the terminal keeps its own when a theme sets none", () => {
    const state = galleryStateOf(themeCatalog(BROKEN), "broken", [appRecord("files")]);
    if (state === null) throw new Error("the catalog has the broken theme");

    const plain = mountView(() => m(ThemeGallery, { state }));
    expect(plain.querySelector("[data-gallery-terminal]")?.getAttribute("data-gallery-terminal")).toBe("own");
    unmountViews();

    document.documentElement.style.setProperty("--term-background", "#000000");
    try {
      const themed = mountView(() => m(ThemeGallery, { state }));
      expect(themed.querySelector("[data-gallery-terminal]")?.getAttribute("data-gallery-terminal")).toBe("theme");
    } finally {
      document.documentElement.style.removeProperty("--term-background");
    }
  });
});
