// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";

import { ICON_NAMES } from "@imbue/workspace-ui/src/components/icons";

import { readDesignTokens } from "../models/tokens";
import { BASE_CSS as baseCss } from "../testing/baseCss";
import { BADGE_TONES, BUTTON_VARIANTS } from "./components";
import { StyleGuidePage } from "./StyleGuidePage";
import { PART_SECTIONS } from "./theming";
import { PARTS } from "@imbue/workspace-ui/src/themes/parts";

afterEach(() => unmountViews());

const tokens = readDesignTokens(baseCss);

function mountPage(): HTMLElement {
  return mountView(() => m(StyleGuidePage, { tokens }));
}

function click(element: Element | null): void {
  if (element === null) throw new Error("nothing to click");
  element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  m.redraw.sync();
}

describe("StyleGuidePage", () => {
  it("lists every section in the contents, each linking to a section that exists", () => {
    const root = mountPage();
    const targets = Array.from(
      root.querySelectorAll<HTMLElement>("[data-toc-target]"),
      (link) => link.dataset.tocTarget,
    );
    expect(targets).toContain("colors");
    expect(targets).toContain("buttons");
    for (const target of targets) expect(root.querySelector(`#${target}`), target).not.toBeNull();
  });

  it("draws a swatch for every color token, painted with its own value", () => {
    const root = mountPage();
    const colors = tokens.colorGroups.flatMap((group) => group.colors);
    expect(root.querySelectorAll(".color-swatch")).toHaveLength(colors.length);
    const accent = root.querySelector<HTMLElement>("#color-accent [data-color-sample]");
    expect(accent?.style.backgroundColor).toBe("var(--c-accent)");
    expect(root.querySelector("#color-accent")?.textContent).toContain("bg-accent");
  });

  it("draws every icon of the shared set and every button variant and badge tone", () => {
    const root = mountPage();
    for (const name of ICON_NAMES) expect(root.querySelector(`#icon-${name} svg`), name).not.toBeNull();
    for (const variant of BUTTON_VARIANTS)
      expect(root.querySelector(`#button-${variant}.btn--${variant}`)).not.toBeNull();
    for (const tone of BADGE_TONES) expect(root.querySelector(`#badge-${tone}.badge`)).not.toBeNull();
  });

  it("opens the menu from its trigger, with a submenu row stating its value", () => {
    const root = mountPage();
    click(root.querySelector("#demo-menu-trigger"));
    const menu = document.querySelector('[data-menu-part="menu"]');
    expect(menu).not.toBeNull();
    expect(menu?.querySelector('[data-menu-row="model"]')?.textContent).toContain("claude-sonnet");
    expect(menu?.querySelector('[data-menu-row="delete"]')).not.toBeNull();
  });

  it("opens each dialog and closes it again from its own button", () => {
    const root = mountPage();
    click(root.querySelector("#open-destroy"));
    expect(document.querySelector(".modal-card")?.textContent).toContain("Quarterly report");
    click(document.querySelector(".destroy-dialog-btn-cancel"));
    expect(document.querySelector(".modal-card")).toBeNull();

    click(root.querySelector("#open-modal"));
    expect(document.querySelector(".modal-title")?.textContent).toBe("Rename project");
  });

  it("shows a toast on request", () => {
    const root = mountPage();
    click(root.querySelector("#show-toast"));
    expect(document.querySelector("[data-toast]")?.textContent).toContain("the disk is full");
  });

  it("moves the segmented control's choice on a click", () => {
    const root = mountPage();
    click(root.querySelector('[data-segment="list"]'));
    expect(root.querySelector('[data-segment="list"]')?.getAttribute("aria-checked")).toBe("true");
    expect(root.querySelector('[data-segment="grid"]')?.getAttribute("aria-checked")).toBe("false");
  });

  it("lists every theme part, linking each the catalog shows to a section that exists", () => {
    const root = mountPage();
    for (const part of PARTS) expect(root.querySelector(`#part-${part}`), part).not.toBeNull();
    for (const section of Object.values(PART_SECTIONS))
      expect(root.querySelector(`#${section}`), section).not.toBeNull();
  });

  it("marks the controls it draws as the parts a theme styles them by", () => {
    const root = mountPage();
    for (const part of ["button", "field", "select", "badge"]) {
      expect(
        root.querySelector(`#${PART_SECTIONS[part as keyof typeof PART_SECTIONS]} [data-part="${part}"]`),
        part,
      ).not.toBeNull();
    }
    click(root.querySelector("#open-modal"));
    for (const part of ["dialog", "dialog-header", "dialog-title", "dialog-actions"])
      expect(document.querySelector(`[data-part="${part}"]`), part).not.toBeNull();
  });

  it("names the theme the page wears, standard when none is marked", () => {
    expect(mountPage().querySelector("#worn-theme")?.textContent).toContain("standard");
    unmountViews();
    document.documentElement.setAttribute("data-ui-theme", "windows-2000");
    try {
      expect(mountPage().querySelector("#worn-theme")?.textContent).toContain("windows-2000");
    } finally {
      document.documentElement.removeAttribute("data-ui-theme");
    }
  });

  it("shows a value a theme changed beside the standard one, struck through, and strikes nothing unthemed", () => {
    expect(mountPage().querySelectorAll(".line-through")).toHaveLength(0);
    unmountViews();
    const theme = document.createElement("style");
    theme.setAttribute("data-workspace-theme", "");
    theme.textContent = ":root { --c-accent: #000080; }";
    document.head.appendChild(theme);
    try {
      const root = mountPage();
      const swatch = root.querySelector("#color-accent");
      expect(swatch?.textContent).toContain("#000080");
      expect(swatch?.querySelector(".line-through")?.textContent).toBe("#2f6b4f");
      expect(root.querySelectorAll(".line-through")).toHaveLength(1);
    } finally {
      theme.remove();
    }
  });

  it("shows a tooltip with a quieter second line", () => {
    const button = mountPage().querySelector("#tooltip-with-detail");
    expect(button?.getAttribute("data-hover-tooltip-detail")).toBe("data/reports/q4-summary.md");
  });

  it("frames the plain page kit sample, taking the page's theme, and names sections by their contents label", () => {
    const root = mountPage();
    const frameElement = root.querySelector("#page-kit-frame");
    expect(frameElement?.getAttribute("src")).toBe("/page-kit-sample");
    expect(frameElement?.hasAttribute("data-theme-relay")).toBe(true);
    expect(root.querySelector('#theme-parts [data-part-section="buttons"]')?.textContent).toBe("Shown under Buttons");
    expect(root.querySelector('#theme-parts [data-part-section="page-kit"]')?.textContent).toBe(
      "Shown under Plain pages",
    );
  });
});
