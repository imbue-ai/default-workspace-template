// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { catalogTemplateRecord } from "../testing/records";
import type { TemplateCatalog } from "./TemplateCatalog";
import {
  getTemplateCatalogState,
  resolveShelves,
  retryTemplateCatalog,
  searchTemplates,
  writeUpParagraphs,
} from "./TemplateCatalog";

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

describe("the catalog's Try again", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("asks the app to fetch afresh rather than answer from its hold after a failure, and shows what comes back", async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(
      async () => new Response(JSON.stringify({ catalog: CATALOG, is_stale: true })),
    );
    vi.stubGlobal("fetch", fetchMock);

    retryTemplateCatalog();

    expect(getTemplateCatalogState()).toEqual({ kind: "loading" });
    await vi.waitFor(() => expect(getTemplateCatalogState()).toEqual({ kind: "loaded", catalog: CATALOG }));
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["/api/templates-catalog?retry=1"]);
  });
});
