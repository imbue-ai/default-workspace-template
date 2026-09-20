import { describe, expect, it } from "vitest";

import { catalogTemplateRecord } from "../testing/records";
import type { TemplateCatalog } from "./TemplateCatalog";
import { resolveShelves, searchTemplates, writeUpParagraphs } from "./TemplateCatalog";

const CATALOG: TemplateCatalog = {
  generated_at: "",
  templates: [
    catalogTemplateRecord("inbox", { title: "Inbox Digest", description: "Triage your mail.", author: "kanjun" }),
    catalogTemplateRecord("radar", {
      title: "Weekend Radar",
      description: "Kid-friendly events near home.",
      author: "matt",
    }),
    catalogTemplateRecord("orchard", { title: "Orchard", description: "A work tracker.", author: "mango" }),
  ],
  shelves: [
    { key: "popular", title: "Most popular", slugs: ["radar", "inbox"] },
    { key: "ghosts", title: "Nothing here", slugs: ["gone"] },
    { key: "work", title: "Work", slugs: ["orchard", "gone"] },
  ],
};

describe("resolveShelves", () => {
  it("keeps the catalog's order, drops unknown slugs and empty rows, and ends on All templates", () => {
    const shelves = resolveShelves(CATALOG);
    expect(shelves.map((shelf) => shelf.key)).toEqual(["popular", "work", "all"]);
    expect(shelves[0].templates.map((t) => t.slug)).toEqual(["radar", "inbox"]);
    expect(shelves[1].templates.map((t) => t.slug)).toEqual(["orchard"]);
    expect(shelves[2].title).toBe("All templates");
    expect(shelves[2].templates.map((t) => t.slug)).toEqual(["inbox", "radar", "orchard"]);
  });

  it("gives a catalog with no shelves the one All templates row", () => {
    expect(resolveShelves({ ...CATALOG, shelves: [] }).map((shelf) => shelf.key)).toEqual(["all"]);
  });
});

describe("searchTemplates", () => {
  it("finds templates by title, description, or author", () => {
    expect(searchTemplates(CATALOG.templates, "digest").map((t) => t.slug)).toEqual(["inbox"]);
    expect(searchTemplates(CATALOG.templates, "events").map((t) => t.slug)).toEqual(["radar"]);
    expect(searchTemplates(CATALOG.templates, "mango").map((t) => t.slug)).toEqual(["orchard"]);
    expect(searchTemplates(CATALOG.templates, "zzz")).toEqual([]);
  });
});

describe("writeUpParagraphs", () => {
  it("unwraps hard-wrapped lines and splits on blank lines", () => {
    expect(writeUpParagraphs("A first\nparagraph.\n\n  A second\none.\n\n\n")).toEqual([
      "A first paragraph.",
      "A second one.",
    ]);
    expect(writeUpParagraphs("")).toEqual([]);
  });
});
