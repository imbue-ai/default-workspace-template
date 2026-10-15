// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { TemplateCatalogState } from "../models/TemplateCatalog";
import { catalogTemplateRecord } from "../testing/records";
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
  catalog: {
    generated_at: "",
    templates: [
      catalogTemplateRecord("inbox", { title: "Inbox Digest", description: "Triage your mail.", author: "kanjun" }),
      catalogTemplateRecord("orchard", { title: "Orchard", description: "A work tracker template.", author: "mango" }),
    ],
    shelves: [{ key: "popular", title: "Most popular", slugs: ["inbox"] }],
  },
};

interface Rendered {
  readonly root: HTMLElement;
  readonly started: string[];
  readonly retries: number[];
  readonly bugReports: number[];
}

function render(catalog: TemplateCatalogState = LOADED, canReportBug = true): Rendered {
  const started: string[] = [];
  const retries: number[] = [];
  const bugReports: number[] = [];
  const root = mountView(() =>
    m(GettingStartedPage, {
      catalog,
      onStartWithText: (text) => started.push(text),
      onRetryTemplates: () => retries.push(retries.length + 1),
      connection: { canReportBug: () => canReportBug, reportBug: () => bugReports.push(bugReports.length + 1) },
    }),
  );
  return { root, started, retries, bugReports };
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
    expect(failed.root.querySelector(".getting-started-templates-status")!.textContent).toBe(
      "Templates couldn't be loaded. Try again. If this persists, report a bug.",
    );
    // Its next step is on the line: Try again fetches again, in place.
    failed.root.querySelector<HTMLElement>(".getting-started-templates-retry")!.click();
    expect(failed.retries).toEqual([1]);
    const report = failed.root.querySelector<HTMLElement>(".getting-started-templates-report")!;
    expect(report.textContent).toBe("Report a bug");
    report.click();
    expect(failed.bugReports).toEqual([1]);
    unmountViews();
    // Where the shell says Imbue Studio opens no bug report, the line offers none.
    const unreportable = render({ kind: "failed" }, false);
    expect(unreportable.root.querySelector(".getting-started-templates-report")).toBeNull();
    unmountViews();
    const disabled = render({ kind: "disabled" });
    expect(disabled.root.querySelector('[data-section="templates"]')).toBeNull();
    expect(disabled.root.querySelector('[data-start="template"]')!.getAttribute("aria-disabled")).toBe("true");
  });
});
