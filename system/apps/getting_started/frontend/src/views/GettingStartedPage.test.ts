// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import type { ReportedFailure } from "@imbue/workspace-ui/src/app_contract";
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
  readonly bugReports: ReportedFailure[];
  readonly asks: string[];
}

function render(catalog: TemplateCatalogState = LOADED, canReportBug = true): Rendered {
  const started: string[] = [];
  const retries: number[] = [];
  const bugReports: ReportedFailure[] = [];
  const asks: string[] = [];
  const root = mountView(() =>
    m(GettingStartedPage, {
      catalog,
      onStartWithText: (text) => started.push(text),
      onRetryTemplates: () => retries.push(retries.length + 1),
      connection: {
        hasShell: () => true,
        askAgent: (text) => {
          asks.push(text);
          return Promise.resolve({ isOpened: true });
        },
        canReportBug: () => canReportBug,
        reportBug: (failure) => bugReports.push(failure),
      },
    }),
  );
  return { root, started, retries, bugReports, asks };
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
    const failed = render({ kind: "failed", details: "HTTP 502" });
    expect(failed.root.querySelector(".getting-started-templates-status")!.textContent).toBe(
      "Templates couldn't be loaded. Try again. If this persists, report a bug.",
    );
    // The reason waits behind a collapsed "See details" under the line.
    const seeDetails = failed.root.querySelector<HTMLDetailsElement>(".getting-started-templates-failed details")!;
    expect(seeDetails.open).toBe(false);
    expect(seeDetails.querySelector("summary")!.textContent).toBe("See details");
    expect(seeDetails.querySelector("pre")!.textContent).toBe("HTTP 502");
    // Its next step is on the line: Try again fetches again, in place.
    failed.root.querySelector<HTMLElement>(".getting-started-templates-retry")!.click();
    expect(failed.retries).toEqual([1]);
    // Then an agent, asked in the chat about this page's app with the failure, and Report a bug.
    const remedies = [...failed.root.querySelectorAll<HTMLElement>(".getting-started-templates-remedy")];
    expect(remedies.map((button) => button.textContent)).toEqual(["Ask an agent to fix this", "Report a bug"]);
    remedies[0].click();
    expect(failed.asks).toHaveLength(1);
    expect(failed.asks[0]).toContain(
      "Imbue Studio showed the user this failure: Templates couldn't be loaded. Try again. If this persists, " +
        "report a bug.",
    );
    expect(failed.asks[0]).toContain("HTTP 502");
    const report = remedies[1];
    report.click();
    expect(failed.bugReports).toEqual([
      { message: "Templates couldn't be loaded. Try again. If this persists, report a bug.", details: "HTTP 502" },
    ]);
    unmountViews();
    // Where the shell says Imbue Studio opens no bug report, the line offers none, and still offers the agent.
    const unreportable = render({ kind: "failed", details: "HTTP 502" }, false);
    expect(
      [...unreportable.root.querySelectorAll(".getting-started-templates-remedy")].map((button) => button.textContent),
    ).toEqual(["Ask an agent to fix this"]);
    unmountViews();
    const disabled = render({ kind: "disabled" });
    expect(disabled.root.querySelector('[data-section="templates"]')).toBeNull();
    expect(disabled.root.querySelector('[data-start="template"]')!.getAttribute("aria-disabled")).toBe("true");
  });
});
