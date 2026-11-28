// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { TemplateCatalogState } from "../models/TemplateCatalog";
import { catalogAppRecord, catalogTemplateRecord } from "../testing/records";
import { GettingStartedPage } from "./GettingStartedPage";
import { START_OPTIONS, START_PAGE_SIZE } from "./startSomething";

// jsdom has no scrollIntoView; the scroll test stands one in and puts this back.
const originalScrollIntoView = Element.prototype.scrollIntoView;

afterEach(() => {
  unmountViews();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

const LOADED: TemplateCatalogState = {
  kind: "loaded",
  isStale: false,
  catalog: {
    generated_at: "",
    templates: [
      catalogTemplateRecord("inbox", { title: "Inbox Digest", description: "Triage your mail.", author: "kanjun" }),
      catalogTemplateRecord("orchard", { title: "Orchard", description: "A work tracker template.", author: "mango" }),
    ],
    apps: [],
    shelves: [{ key: "popular", title: "Most popular", slugs: ["inbox"] }],
  },
};

// The same catalog once it lists a published app, named on the shelf beside the template.
const LOADED_WITH_APPS: TemplateCatalogState = {
  kind: "loaded",
  isStale: false,
  catalog: {
    ...LOADED.catalog,
    apps: [catalogAppRecord("weather-desk", { title: "Weather Desk", description: "Forecasts.", author: "sky" })],
    shelves: [{ key: "popular", title: "Most popular", slugs: ["app:weather-desk", "inbox", "app:not-listed"] }],
  },
};

function render(catalog: TemplateCatalogState = LOADED): { root: HTMLElement; started: string[] } {
  const started: string[] = [];
  const root = mountView(() => m(GettingStartedPage, { catalog, onStartWithText: (text) => started.push(text) }));
  return { root, started };
}

function typeQuery(root: HTMLElement, query: string): void {
  const input = root.querySelector<HTMLInputElement>("[data-getting-started-search]")!;
  input.value = query;
  input.dispatchEvent(new InputEvent("input", { bubbles: true }));
  m.redraw.sync();
}

describe("the Getting Started page", () => {
  it("shows the first page of tiles, See more reveals the rest, and a tile starts a chat with its prompt", () => {
    const { root, started } = render();
    expect(root.querySelectorAll("[data-start]")).toHaveLength(START_PAGE_SIZE);
    root.querySelector<HTMLElement>(".getting-started-more")!.click();
    m.redraw.sync();
    expect(root.querySelectorAll("[data-start]")).toHaveLength(START_OPTIONS.length);
    expect(root.querySelector(".getting-started-more")).toBeNull();

    root.querySelector<HTMLElement>('[data-start="build-app"]')!.click();
    expect(started).toEqual([START_OPTIONS.find((option) => option.key === "build-app")!.prompt]);
  });

  it("shows the shelves, and a card opens the detail page whose back control returns", () => {
    const { root, started } = render();
    expect(root.querySelector('[data-shelf="popular"]')).not.toBeNull();
    expect(root.querySelector('[data-shelf="all"]')).not.toBeNull();
    root.querySelector<HTMLElement>('[data-shelf="popular"] [data-template="inbox"]')!.click();
    m.redraw.sync();
    expect(root.querySelector('.new-tab-template-detail[data-template="inbox"]')).not.toBeNull();
    expect(root.querySelector("[data-getting-started-search]")).toBeNull();
    root.querySelector<HTMLElement>(".new-tab-template-adopt")!.click();
    expect(started).toEqual(["/use-template https://github.com/someone/inbox"]);
    root.querySelector<HTMLElement>(".new-tab-template-back")!.click();
    m.redraw.sync();
    expect(root.querySelector(".new-tab-template-detail")).toBeNull();
    expect(root.querySelector("[data-getting-started-search]")).not.toBeNull();
  });

  it("searching narrows the tiles and the templates, says when nothing matches, and Escape clears", () => {
    const { root } = render();
    typeQuery(root, "routine");
    expect(Array.from(root.querySelectorAll("[data-start]")).map((tile) => tile.getAttribute("data-start"))).toEqual([
      "routine",
    ]);
    expect(root.querySelector("[data-template]")).toBeNull();
    typeQuery(root, "kanjun");
    expect(root.querySelectorAll("[data-start]")).toHaveLength(0);
    expect(root.querySelector('[data-template="inbox"]')).not.toBeNull();
    expect(root.querySelector('[data-template="orchard"]')).toBeNull();
    typeQuery(root, "zzzz");
    expect(root.querySelector(".getting-started-no-matches")).not.toBeNull();
    const input = root.querySelector<HTMLInputElement>("[data-getting-started-search]")!;
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(root.querySelector(".getting-started-no-matches")).toBeNull();
    expect(root.querySelectorAll("[data-start]")).toHaveLength(START_PAGE_SIZE);
  });

  it("the template tile scrolls to the templates on hand, resting or searched, and owes nothing when there are none", () => {
    const scrolled: string[] = [];
    Element.prototype.scrollIntoView = function (this: Element) {
      scrolled.push(this.getAttribute("data-section") ?? "");
    };
    const { root } = render();
    root.querySelector<HTMLElement>('[data-start="template"]')!.click();
    m.redraw.sync();
    expect(scrolled).toEqual(["templates"]);

    // These results hold the tile and no template: the pick has nowhere to scroll, now or later.
    typeQuery(root, "start from a");
    expect(root.querySelector('[data-section="templates"]')).toBeNull();
    root.querySelector<HTMLElement>('[data-start="template"]')!.click();
    m.redraw.sync();
    typeQuery(root, "");
    expect(root.querySelector('[data-section="templates"]')).not.toBeNull();
    expect(scrolled).toEqual(["templates"]);

    // These results hold both: the tile scrolls the results' own templates section.
    typeQuery(root, "template");
    expect(root.querySelector('[data-section="templates"] [data-template="orchard"]')).not.toBeNull();
    root.querySelector<HTMLElement>('[data-start="template"]')!.click();
    m.redraw.sync();
    expect(scrolled).toEqual(["templates", "templates"]);
  });

  it("says when the templates are loading or failed, and omits the section (and stands the template tile down) with no catalog", () => {
    const loading = render({ kind: "loading" });
    expect(loading.root.querySelector(".getting-started-templates-status")!.textContent).toContain("Loading");
    unmountViews();
    const failed = render({ kind: "failed" });
    expect(failed.root.querySelector(".getting-started-templates-status")!.textContent).toContain("Failed");
    unmountViews();
    const disabled = render({ kind: "disabled" });
    expect(disabled.root.querySelector('[data-section="templates"]')).toBeNull();
    expect(disabled.root.querySelector('[data-start="template"]')!.getAttribute("aria-disabled")).toBe("true");
  });

  it("shows a published app on its shelf and in All apps, and its detail page starts the install chat", () => {
    const { root, started } = render(LOADED_WITH_APPS);
    expect(root.querySelector('[data-section="templates"] h2')!.textContent).toBe("Start from a template or an app");
    const popular = root.querySelector<HTMLElement>('[data-shelf="popular"]')!;
    expect(
      Array.from(popular.querySelectorAll(".new-tab-template-card")).map(
        (card) => card.getAttribute("data-app") ?? card.getAttribute("data-template"),
      ),
    ).toEqual(["weather-desk", "inbox"]);
    expect(root.querySelector('[data-shelf="all-apps"] [data-app="weather-desk"]')).not.toBeNull();
    expect(root.querySelector('[data-shelf="all"] [data-app]')).toBeNull();

    popular.querySelector<HTMLElement>('[data-app="weather-desk"]')!.click();
    m.redraw.sync();
    expect(root.querySelector('.new-tab-template-detail[data-app="weather-desk"]')).not.toBeNull();
    root.querySelector<HTMLElement>(".new-tab-app-install")!.click();
    root.querySelector<HTMLElement>(".new-tab-app-create-workspace")!.click();
    expect(started).toEqual([
      "/install-app https://github.com/someone/weather-desk",
      "Please create a new Imbue Studio workspace for me with the app at https://github.com/someone/weather-desk " +
        "installed in it (the imbue-studio skill can create one). Walk me through anything it needs from me, like " +
        "permissions or accounts, and tell me when it is ready.",
    ]);
    root.querySelector<HTMLElement>(".new-tab-template-back")!.click();
    m.redraw.sync();
    expect(root.querySelector(".new-tab-template-detail")).toBeNull();
  });

  it("a catalog with no apps shows no apps row and keeps the templates heading", () => {
    const { root } = render();
    expect(root.querySelector('[data-section="templates"] h2')!.textContent).toBe("Start from a template");
    expect(root.querySelector('[data-shelf="all-apps"]')).toBeNull();
    expect(root.querySelector("[data-app]")).toBeNull();
  });

  it("searching finds a published app beside the templates", () => {
    const { root } = render(LOADED_WITH_APPS);
    typeQuery(root, "forecasts");
    expect(root.querySelector('[data-section="templates"] h2')!.textContent).toBe("Templates and apps");
    expect(root.querySelector('[data-section="templates"] [data-app="weather-desk"]')).not.toBeNull();
    expect(root.querySelector("[data-template]")).toBeNull();
    typeQuery(root, "kanjun");
    expect(root.querySelector('[data-section="templates"] h2')!.textContent).toBe("Templates");
    expect(root.querySelector("[data-app]")).toBeNull();
  });
});
