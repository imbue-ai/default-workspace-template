// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import m from "mithril";

import { catalogTemplateRecord } from "../testing/records";
import type { CatalogTemplate, ResolvedShelf } from "../models/TemplateCatalog";
import { TemplateCard, TemplateShelves, railPageTarget, railPaging } from "./TemplateShelves";

describe("rail paging", () => {
  it("offers an arrow only where there is somewhere to go", () => {
    expect(railPaging({ scrollLeft: 0, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: false,
      canPageRight: true,
    });
    expect(railPaging({ scrollLeft: 400, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: true,
      canPageRight: true,
    });
    expect(railPaging({ scrollLeft: 900, clientWidth: 600, scrollWidth: 1500 })).toEqual({
      canPageLeft: true,
      canPageRight: false,
    });
    expect(railPaging({ scrollLeft: 0, clientWidth: 600, scrollWidth: 600 })).toEqual({
      canPageLeft: false,
      canPageRight: false,
    });
  });

  it("pages one visible width along, clamped to the ends", () => {
    expect(railPageTarget({ scrollLeft: 0, clientWidth: 600, scrollWidth: 1500 }, 1)).toBe(600);
    expect(railPageTarget({ scrollLeft: 600, clientWidth: 600, scrollWidth: 1500 }, 1)).toBe(900);
    expect(railPageTarget({ scrollLeft: 900, clientWidth: 600, scrollWidth: 1500 }, -1)).toBe(300);
    expect(railPageTarget({ scrollLeft: 300, clientWidth: 600, scrollWidth: 1500 }, -1)).toBe(0);
  });
});

describe("TemplateCard", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  function mountCard(template: CatalogTemplate, isFill = false): void {
    m.mount(root, { view: () => m(TemplateCard, { template, isFill, onPick: () => undefined }) });
  }

  it("says what the template is, not just what it is called", () => {
    mountCard(
      catalogTemplateRecord("orchard", {
        title: "Orchard",
        description: "A recruiter's candidate pipeline with templated outreach.",
        author: "mangonomnom",
      }),
    );
    const card = root.querySelector(".new-tab-template-card")!;
    expect(card.querySelector(".new-tab-template-title")!.textContent).toBe("Orchard");
    expect(card.querySelector(".new-tab-template-description")!.textContent).toBe(
      "A recruiter's candidate pipeline with templated outreach.",
    );
    expect(card.textContent).toContain("by mangonomnom");
  });

  it("keeps the description to two lines, and keeps those two lines when there is no description", () => {
    // The rail lays cards out side by side, so a card whose description ran on -- or vanished --
    // would push its byline off the line its neighbours' sit on.
    for (const description of ["A ".repeat(200), "Short.", ""]) {
      mountCard(catalogTemplateRecord("orchard", { description }));
      const paragraph = root.querySelector(".new-tab-template-description")!;
      expect(paragraph.textContent).toBe(description);
      expect(paragraph.className).toContain("line-clamp-2");
      expect(paragraph.className).toContain("min-h-[2lh]");
    }
  });

  it("runs one type ramp down the card: title, then description, then byline", () => {
    mountCard(catalogTemplateRecord("orchard"));
    const card = root.querySelector(".new-tab-template-card")!;
    // Each step is a role utility plus a colour, never a hand-set size: the title carries the
    // weight, the description drops a size, the byline drops the last of the contrast.
    expect(card.querySelector(".new-tab-template-title")!.className).toContain("type-label");
    expect(card.querySelector(".new-tab-template-title")!.className).toContain("text-primary");
    expect(card.querySelector(".new-tab-template-description")!.className).toContain("type-helper");
    expect(card.querySelector(".new-tab-template-description")!.className).toContain("text-secondary");
    const byline = card.lastElementChild!;
    expect(byline.textContent).toBe("by someone");
    expect(byline.className).toContain("type-helper");
    expect(byline.className).toContain("text-faint");
    expect(card.innerHTML).not.toMatch(/text-\[\d/);
  });

  it("drops only the byline for a template published by nobody", () => {
    mountCard(catalogTemplateRecord("orchard", { author: "" }));
    const card = root.querySelector(".new-tab-template-card")!;
    expect(card.textContent).not.toContain("by ");
    expect(card.querySelector(".new-tab-template-description")).not.toBeNull();
  });

  it("fills its cell as a search result and takes the rail's card width in a shelf", () => {
    mountCard(catalogTemplateRecord("orchard"), true);
    expect(root.querySelector(".new-tab-template-card")!.className).toContain("w-full");

    mountCard(catalogTemplateRecord("orchard"), false);
    const railCard = root.querySelector(".new-tab-template-card")!;
    expect(railCard.className).not.toContain("w-full");
    // Two and a half cards across, so the sliced one says the rail scrolls.
    expect(railCard.className).toContain("w-[calc((100%-48px)/2.5)]");
  });
});

describe("TemplateShelves", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  it("heads each rail a level above the cards in it", () => {
    const shelves: ResolvedShelf[] = [
      { key: "popular", title: "Most popular", templates: [catalogTemplateRecord("orchard")] },
      { key: "all", title: "All templates", templates: [catalogTemplateRecord("orchard")] },
    ];
    m.mount(root, { view: () => m(TemplateShelves, { shelves, onPick: () => undefined }) });

    const headings = root.querySelectorAll(".new-tab-template-shelf-title");
    expect([...headings].map((heading) => heading.textContent)).toEqual(["Most popular", "All templates"]);
    for (const heading of headings) {
      expect(heading.tagName).toBe("H3");
      // ``type-heading`` (18px) over the card title's ``type-label`` (14px): the row's name has to
      // win by a size, not by weight alone.
      expect(heading.className).toContain("type-heading");
      expect(heading.className).not.toContain("type-heading-lg");
      expect(heading.className).not.toContain("type-label");
    }
    // Every rail but the first stands off from the one above it, so a row reads as its own tray.
    const shelfSections = root.querySelectorAll(".new-tab-template-shelf");
    expect(shelfSections[1].className).toContain("mt-6");
    expect(shelfSections[0].className).toContain("first:mt-0");
  });
});
