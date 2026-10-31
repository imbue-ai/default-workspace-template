// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { currentSectionId, keepCurrentEntryInView } from "./StyleGuidePage";

/** An element whose box jsdom reports as ``top``..``top + height`` (jsdom lays nothing out). */
function boxed(id: string, top: number, height = 20): HTMLElement {
  const element = document.createElement("div");
  element.id = id;
  element.getBoundingClientRect = () => ({
    top,
    bottom: top + height,
    left: 0,
    right: 0,
    width: 0,
    height,
    x: 0,
    y: top,
    toJSON: () => ({}),
  });
  return element;
}

function scroller(top: number, scrollTop: number, clientHeight: number, scrollHeight: number): HTMLElement {
  const element = boxed("scroller", top, clientHeight);
  Object.defineProperty(element, "scrollTop", { value: scrollTop, writable: true });
  Object.defineProperty(element, "clientHeight", { value: clientHeight });
  Object.defineProperty(element, "scrollHeight", { value: scrollHeight });
  return element;
}

describe("currentSectionId", () => {
  it("is the last section whose top has passed the line near the top, not the one above a section just jumped to", () => {
    // Buttons is still partly on screen above; Text fields was jumped to and sits just under the top.
    const targets = [boxed("buttons", -300), boxed("fields", 32), boxed("pickers", 600)];
    expect(currentSectionId(scroller(0, 900, 500, 5000), targets)).toBe("fields");
  });

  it("is the first section before any has passed the line, and the last once the page is scrolled to its end", () => {
    const targets = [boxed("colors", 300), boxed("type", 900), boxed("page-kit", 1400)];
    expect(currentSectionId(scroller(0, 0, 500, 5000), targets)).toBe("colors");
    expect(currentSectionId(scroller(0, 4500, 500, 5000), targets)).toBe("page-kit");
  });
});

describe("keepCurrentEntryInView", () => {
  function navWith(entryTop: number, scrollTop: number): HTMLElement {
    const nav = boxed("nav", 0, 400);
    Object.defineProperty(nav, "scrollTop", { value: scrollTop, writable: true });
    const entry = boxed("entry", entryTop);
    entry.setAttribute("aria-current", "location");
    nav.appendChild(entry);
    return nav;
  }

  it("scrolls the column down to an entry below its bottom, up to one above its top, and leaves one in view alone", () => {
    const below = navWith(450, 100);
    keepCurrentEntryInView(below);
    // The entry's bottom (470) comes to 24 above the column's bottom (376).
    expect(below.scrollTop).toBe(194);

    const above = navWith(-60, 300);
    keepCurrentEntryInView(above);
    expect(above.scrollTop).toBe(216);

    const inView = navWith(150, 40);
    keepCurrentEntryInView(inView);
    expect(inView.scrollTop).toBe(40);
  });
});
