// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import m from "mithril";

import { catalogAppRecord, catalogTemplateRecord } from "../testing/records";
import type { CatalogListing, CatalogTemplate } from "../models/TemplateCatalog";
import { appListing, templateListing } from "../models/TemplateCatalog";
import { TemplateArt } from "./TemplateArt";

describe("TemplateArt", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  function mountListingArt(listing: CatalogListing): void {
    m.mount(root, { view: () => m(TemplateArt, { listing, frameClass: "rounded-lg", glyphSize: 20 }) });
  }

  function mountArt(template: CatalogTemplate): void {
    mountListingArt(templateListing(template));
  }

  it("shows the drawing, and the glyph once it fails to load", () => {
    mountArt(catalogTemplateRecord("orchard"));
    const img = root.querySelector<HTMLImageElement>("img")!;
    expect(img.getAttribute("src")).toBe("https://example.test/orchard.svg");
    expect(root.querySelector("svg")).toBeNull();

    img.dispatchEvent(new Event("error"));
    m.redraw.sync();
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("svg")).not.toBeNull();
  });

  it("shows the glyph from the start for a template with no drawing", () => {
    mountArt(catalogTemplateRecord("plain", { thumbnail_url: "" }));
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("svg")).not.toBeNull();
  });

  it("remembers the broken drawing by template, so another template's drawing still shows", () => {
    let template = catalogTemplateRecord("orchard");
    m.mount(root, {
      view: () => m(TemplateArt, { listing: templateListing(template), frameClass: "", glyphSize: 20 }),
    });
    root.querySelector<HTMLImageElement>("img")!.dispatchEvent(new Event("error"));
    m.redraw.sync();
    expect(root.querySelector("img")).toBeNull();

    template = catalogTemplateRecord("radar");
    m.redraw.sync();
    expect(root.querySelector<HTMLImageElement>("img")!.getAttribute("src")).toBe("https://example.test/radar.svg");
  });

  it("shows an app's icon whole inside the frame rather than cropped to it", () => {
    mountListingArt(appListing(catalogAppRecord("weather-desk")));
    const img = root.querySelector<HTMLImageElement>("img")!;
    expect(img.getAttribute("src")).toBe("https://example.test/app-icons/weather-desk.svg");
    expect(img.className).toContain("object-contain");
    expect(img.className).not.toContain("object-cover");
  });

  it("shows the glyph for an app with no icon, and for one whose icon fails to load", () => {
    mountListingArt(appListing(catalogAppRecord("bare", { icon_url: "" })));
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("svg")).not.toBeNull();

    mountListingArt(appListing(catalogAppRecord("weather-desk")));
    root.querySelector<HTMLImageElement>("img")!.dispatchEvent(new Event("error"));
    m.redraw.sync();
    expect(root.querySelector("img")).toBeNull();
    expect(root.querySelector("svg")).not.toBeNull();
  });

  it("keeps a template's drawing when an app of the same name has a broken icon", () => {
    let listing: CatalogListing = appListing(catalogAppRecord("orchard"));
    m.mount(root, { view: () => m(TemplateArt, { listing, frameClass: "", glyphSize: 20 }) });
    root.querySelector<HTMLImageElement>("img")!.dispatchEvent(new Event("error"));
    m.redraw.sync();
    expect(root.querySelector("img")).toBeNull();

    listing = templateListing(catalogTemplateRecord("orchard"));
    m.redraw.sync();
    expect(root.querySelector<HTMLImageElement>("img")!.getAttribute("src")).toBe("https://example.test/orchard.svg");
  });
});
