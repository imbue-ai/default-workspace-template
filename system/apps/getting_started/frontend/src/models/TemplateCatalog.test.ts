import { describe, expect, it } from "vitest";

import { catalogAppRecord, catalogTemplateRecord } from "../testing/records";
import type { ResolvedShelf, TemplateCatalog } from "./TemplateCatalog";
import {
  catalogFromWire,
  listingKey,
  resolveShelves,
  searchListings,
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
  apps: [],
  shelves: [
    { key: "popular", title: "Most popular", slugs: ["radar", "inbox"] },
    { key: "ghosts", title: "Nothing here", slugs: ["gone"] },
    { key: "work", title: "Work", slugs: ["orchard", "gone"] },
  ],
};

// The same catalog once apps are published: two apps, one of them on a shelf beside templates.
const CATALOG_WITH_APPS: TemplateCatalog = {
  ...CATALOG,
  apps: [
    catalogAppRecord("weather-desk", { title: "Weather Desk", description: "Forecasts at a glance.", author: "sky" }),
    catalogAppRecord("ledger", { title: "Ledger", description: "Track what you spend.", author: "mango" }),
  ],
  shelves: [
    { key: "popular", title: "Most popular", slugs: ["radar", "app:weather-desk", "inbox"] },
    { key: "ghosts", title: "Nothing here", slugs: ["app:gone", "skill:unknown-kind", "gone"] },
    { key: "money", title: "Money", slugs: ["app:ledger", "app:gone"] },
  ],
};

function keysOf(shelf: ResolvedShelf): string[] {
  return shelf.listings.map(listingKey);
}

describe("resolveShelves", () => {
  it("keeps the catalog's order, drops unknown slugs and empty rows, and ends on All templates", () => {
    const shelves = resolveShelves(CATALOG);
    expect(shelves.map((shelf) => shelf.key)).toEqual(["popular", "work", "all"]);
    expect(keysOf(shelves[0])).toEqual(["radar", "inbox"]);
    expect(keysOf(shelves[1])).toEqual(["orchard"]);
    expect(shelves[2].title).toBe("All templates");
    expect(keysOf(shelves[2])).toEqual(["inbox", "radar", "orchard"]);
    expect(shelves.flatMap((shelf) => shelf.listings).every((listing) => listing.kind === "template")).toBe(true);
  });

  it("gives a catalog with no shelves the one All templates row", () => {
    expect(resolveShelves({ ...CATALOG, shelves: [] }).map((shelf) => shelf.key)).toEqual(["all"]);
  });

  it("resolves a shelf's app entries in place among its templates, and adds an All apps row", () => {
    const shelves = resolveShelves(CATALOG_WITH_APPS);
    expect(shelves.map((shelf) => shelf.key)).toEqual(["popular", "money", "all-apps", "all"]);
    expect(keysOf(shelves[0])).toEqual(["radar", "app:weather-desk", "inbox"]);
    expect(shelves[0].listings.map((listing) => listing.kind)).toEqual(["template", "app", "template"]);
    expect(keysOf(shelves[1])).toEqual(["app:ledger"]);
    expect(shelves[2].title).toBe("All apps");
    expect(keysOf(shelves[2])).toEqual(["app:weather-desk", "app:ledger"]);
    expect(keysOf(shelves[3])).toEqual(["inbox", "radar", "orchard"]);
  });

  it("drops an entry that names no app or template the catalog carries, and a row left empty by that", () => {
    const shelves = resolveShelves(CATALOG_WITH_APPS);
    // "ghosts" held an app the catalog does not list, an entry of a kind this page does not know,
    // and a template that is gone: none of them is shown, and neither is the row.
    expect(shelves.find((shelf) => shelf.key === "ghosts")).toBeUndefined();
    expect(shelves.flatMap(keysOf)).not.toContain("app:gone");
  });

  it("never takes an app's name for a template's slug or the other way round", () => {
    const catalog: TemplateCatalog = {
      ...CATALOG,
      apps: [catalogAppRecord("inbox")],
      shelves: [
        { key: "bare", title: "Bare", slugs: ["inbox"] },
        { key: "prefixed", title: "Prefixed", slugs: ["app:inbox", "app:radar"] },
      ],
    };
    const shelves = resolveShelves(catalog);
    expect(shelves[0].listings.map((listing) => listing.kind)).toEqual(["template"]);
    expect(shelves[1].listings.map((listing) => listing.kind)).toEqual(["app"]);
  });
});

describe("catalogFromWire", () => {
  it("reads a catalog answered without apps as one that lists none", () => {
    const { apps: _apps, ...withoutApps } = CATALOG_WITH_APPS;
    const catalog = catalogFromWire(withoutApps);
    expect(catalog.apps).toEqual([]);
    // Its shelves still name apps; with none listed, those entries are dropped rather than shown.
    expect(resolveShelves(catalog).map((shelf) => shelf.key)).toEqual(["popular", "all"]);
    expect(keysOf(resolveShelves(catalog)[0])).toEqual(["radar", "inbox"]);
  });

  it("keeps the apps a catalog lists", () => {
    expect(catalogFromWire(CATALOG_WITH_APPS).apps.map((app) => app.name)).toEqual(["weather-desk", "ledger"]);
  });
});

describe("searchListings", () => {
  it("finds apps and templates by title, description, or author, apps first", () => {
    expect(searchListings(CATALOG_WITH_APPS, "forecasts").map(listingKey)).toEqual(["app:weather-desk"]);
    expect(searchListings(CATALOG_WITH_APPS, "mango").map(listingKey)).toEqual(["app:ledger", "orchard"]);
    expect(searchListings(CATALOG_WITH_APPS, "digest").map(listingKey)).toEqual(["inbox"]);
    expect(searchListings(CATALOG_WITH_APPS, "zzz")).toEqual([]);
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
