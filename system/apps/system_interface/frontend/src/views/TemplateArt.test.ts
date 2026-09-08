// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import m from "mithril";

import { catalogTemplateRecord } from "../testing/records";
import type { CatalogTemplate } from "../models/TemplateCatalog";
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

  function mountArt(template: CatalogTemplate): void {
    m.mount(root, { view: () => m(TemplateArt, { template, frameClass: "rounded-lg", glyphSize: 20 }) });
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
    m.mount(root, { view: () => m(TemplateArt, { template, frameClass: "", glyphSize: 20 }) });
    root.querySelector<HTMLImageElement>("img")!.dispatchEvent(new Event("error"));
    m.redraw.sync();
    expect(root.querySelector("img")).toBeNull();

    template = catalogTemplateRecord("radar");
    m.redraw.sync();
    expect(root.querySelector<HTMLImageElement>("img")!.getAttribute("src")).toBe("https://example.test/radar.svg");
  });
});
