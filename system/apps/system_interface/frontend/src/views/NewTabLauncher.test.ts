// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { appRecord, catalogTemplateRecord } from "../testing/records";
import type { TemplateCatalog, TemplateCatalogState } from "../models/TemplateCatalog";
import {
  NewTabLauncher,
  adoptTemplateMessage,
  appsInRows,
  appsInSection,
  buildLauncherSections,
  createMachineFromTemplateMessage,
  filterRowsByApp,
  formatRecency,
  promptTargetOfTiles,
  restingSections,
  searchLauncherRows,
  searchTiles,
  sortRowsByRecency,
  orderLaunchTiles,
} from "./NewTabLauncher";
import type { LaunchTile, LauncherRow, NewTabLauncherAttrs } from "./NewTabLauncher";
import { START_OPTIONS, START_PAGE_SIZE } from "./startSomething";
import { HOVER_LIFT_TRANSITION } from "./hoverLift";

function row(
  address: string,
  appName: string,
  lastActiveMs: number | null,
  overrides: Partial<LauncherRow> = {},
): LauncherRow {
  return {
    address,
    appName,
    appDisplayName: appName[0].toUpperCase() + appName.slice(1),
    label: address,
    status: "idle",
    lastActiveMs,
    ...overrides,
  };
}

function tile(name: string, overrides: Partial<LaunchTile["app"]> = {}): LaunchTile {
  const app = appRecord(name, overrides);
  return { app, action: app.actions[0] ?? { id: "new", label: `New ${name}`, params: [] } };
}

/** The chat app as its manifest declares it: ranked first, its ``new`` action taking a message. */
function chatTile(): LaunchTile {
  return tile("chat", {
    critical: true,
    launcher_rank: 10,
    actions: [
      { id: "new", label: "New Chat", params: ["account_id", "message"] },
      { id: "subagent", label: "Open subagent", params: ["parent", "session"] },
    ],
  });
}

const CATALOG: TemplateCatalog = {
  generated_at: "",
  templates: [
    catalogTemplateRecord("inbox-digest"),
    catalogTemplateRecord("weekend-radar"),
    catalogTemplateRecord("orchard"),
  ],
  shelves: [
    { key: "popular", title: "Most popular", slugs: ["inbox-digest", "weekend-radar"] },
    { key: "work", title: "Reimagine your work", slugs: ["orchard", "missing-slug"] },
  ],
};
const LOADED: TemplateCatalogState = { kind: "loaded", catalog: CATALOG, isStale: false };

const MACHINE = [
  row("app:terminal?instance=t1", "terminal", 1_000, { label: "Terminal 1" }),
  row("app:chat?instance=c1", "chat", 5_000, { label: "Chat 1" }),
  row("app:files", "files", null, { label: "File Viewer" }),
];

// Registry order, with the built-ins' manifest ranks: the tiles lead chat, files, browser, terminal.
const TILES = [
  tile("terminal", { launcher_rank: 40 }),
  tile("notes"),
  chatTile(),
  tile("files", { launcher_rank: 20 }),
  tile("browser", { launcher_rank: 30 }),
];

describe("buildLauncherSections", () => {
  it("splits a project's view into its tab set and the rest of the machine", () => {
    const sections = buildLauncherSections(MACHINE, [MACHINE[1]], false);
    expect(sections.map((section) => section.key)).toEqual(["in-project", "on-machine"]);
    expect(sections[0].rows.map((r) => r.address)).toEqual(["app:chat?instance=c1"]);
    expect(sections[1].rows.map((r) => r.address)).toEqual(["app:terminal?instance=t1", "app:files"]);
  });

  it("gives Everything the single machine-wide table", () => {
    const sections = buildLauncherSections(MACHINE, [MACHINE[1]], true);
    expect(sections.map((section) => section.key)).toEqual(["on-machine"]);
    expect(sections[0].rows.length).toBe(3);
  });
});

describe("restingSections", () => {
  it("shows a project only its own tab set, and nothing when that is empty", () => {
    expect(restingSections(MACHINE, [MACHINE[1]], false).map((section) => section.key)).toEqual(["in-project"]);
    expect(restingSections(MACHINE, [], false)).toEqual([]);
  });

  it("shows Everything the machine, and nothing on an empty machine", () => {
    expect(restingSections(MACHINE, [], true).map((section) => section.key)).toEqual(["on-machine"]);
    expect(restingSections([], [], true)).toEqual([]);
  });
});

describe("tile helpers", () => {
  it("leads with the ranked apps in rank order and follows with every unranked app", () => {
    expect(orderLaunchTiles(TILES).map((t) => t.app.name)).toEqual(["chat", "files", "browser", "terminal", "notes"]);
  });

  it("keeps registry order among unranked apps", () => {
    expect(orderLaunchTiles([tile("terminal"), tile("notes")]).map((t) => t.app.name)).toEqual(["terminal", "notes"]);
  });

  it("sends a prompt to the first app whose action takes a message, whatever it is called", () => {
    const target = promptTargetOfTiles(TILES);
    expect(target?.app.name).toBe("chat");
    expect(target?.action.id).toBe("new");
    expect(promptTargetOfTiles([tile("terminal"), tile("notes")])).toBeNull();
    const assistant = tile("assistant", { actions: [{ id: "ask", label: "Ask", params: ["message"] }] });
    expect(promptTargetOfTiles([tile("terminal"), assistant])?.action.id).toBe("ask");
  });
});

describe("search helpers", () => {
  it("finds rows by title or app, every token of the query somewhere", () => {
    expect(searchLauncherRows(MACHINE, "chat").map((r) => r.address)).toEqual(["app:chat?instance=c1"]);
    expect(searchLauncherRows(MACHINE, "viewer file").map((r) => r.address)).toEqual(["app:files"]);
    expect(searchLauncherRows(MACHINE, "nothing here")).toEqual([]);
  });

  it("finds the actions a query can answer with, by the row text they render as", () => {
    expect(searchTiles(TILES, "term").map((t) => t.app.name)).toEqual(["terminal"]);
    expect(searchTiles(TILES, "new").map((t) => t.app.name).length).toBe(TILES.length);
    // What the row displays is what a query finds: "open term" reaches "Open new terminal".
    expect(searchTiles(TILES, "open term").map((t) => t.app.name)).toEqual(["terminal"]);
    expect(searchTiles(TILES, "open").length).toBe(TILES.length);
  });
});

describe("row helpers", () => {
  it("hides the apps a filter unchecked", () => {
    expect(filterRowsByApp(MACHINE, new Set(["chat"])).map((r) => r.appName)).toEqual(["terminal", "files"]);
  });

  it("orders most recent first with unknown recency last", () => {
    expect(sortRowsByRecency(MACHINE).map((r) => r.appName)).toEqual(["chat", "terminal", "files"]);
  });

  it("lists the apps present once each, in first-seen order", () => {
    expect(appsInRows([...MACHINE, MACHINE[0]]).map((entry) => entry.name)).toEqual(["terminal", "chat", "files"]);
  });

  it("lists a table's action-row apps ahead of its instance-row apps, each once", () => {
    const apps = appsInSection(MACHINE, [tile("notes"), tile("chat")]);
    expect(apps.map((entry) => entry.name)).toEqual(["notes", "chat", "terminal", "files"]);
    expect(apps[0].displayName).toBe("Notes");
  });

  it("formats recency coarsely", () => {
    const now = 10 * 24 * 60 * 60 * 1000;
    expect(formatRecency(null, now)).toBe("—");
    expect(formatRecency(now + 5, now)).toBe("just now");
    expect(formatRecency(now - 90_000, now)).toBe("1m ago");
    expect(formatRecency(now - 3 * 60 * 60 * 1000, now)).toBe("3h ago");
    expect(formatRecency(now - 2 * 24 * 60 * 60 * 1000, now)).toBe("2d ago");
    expect(formatRecency(now - 8 * 24 * 60 * 60 * 1000, now)).toBe("last week");
    expect(formatRecency(0, 30 * 24 * 60 * 60 * 1000)).toBe("4w ago");
  });
});

describe("the messages a template's actions seed", () => {
  it("adopts through the use-template skill and asks for a new machine in plain words", () => {
    const orchard = catalogTemplateRecord("orchard");
    expect(adoptTemplateMessage(orchard)).toBe("/use-template https://github.com/someone/orchard");
    expect(createMachineFromTemplateMessage(orchard)).toContain("https://github.com/someone/orchard");
    expect(createMachineFromTemplateMessage(orchard)).toContain("new Minds machine");
  });
});

describe("NewTabLauncher", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  function mount(overrides: Partial<NewTabLauncherAttrs>): NewTabLauncherAttrs {
    const attrs: NewTabLauncherAttrs = {
      tiles: TILES,
      rows: MACHINE,
      memberRows: [MACHINE[1]],
      isEverything: false,
      catalog: LOADED,
      nowMs: 10_000,
      onRunAction: vi.fn(),
      onOpenRow: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(NewTabLauncher, attrs) });
    return attrs;
  }

  function type(text: string): void {
    const input = root.querySelector<HTMLInputElement>(".new-tab-launcher-search input")!;
    input.value = text;
    input.dispatchEvent(new Event("input"));
    m.redraw.sync();
  }

  function sectionKeys(): string[] {
    return Array.from(root.querySelectorAll<HTMLElement>("[data-section]")).map((section) => section.dataset.section!);
  }

  /** Open a table's filter menu and uncheck the app the menu shows as ``displayName``. */
  function uncheckAppInFilter(sectionKey: string, displayName: string): void {
    root.querySelector<HTMLElement>(`[data-section="${sectionKey}"] button[aria-expanded]`)!.click();
    m.redraw.sync();
    const label = Array.from(root.querySelectorAll("label")).find((candidate) =>
      candidate.textContent!.includes(displayName),
    )!;
    label.querySelector("input")!.dispatchEvent(new Event("change"));
    m.redraw.sync();
  }

  it("runs a tile's action with no parameters, whichever app it is", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-launch="terminal:new"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "terminal" }), "new", {});
    root.querySelector<HTMLElement>('[data-launch="chat:new"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "chat" }), "new", {});
  });

  it("lays the tiles out as one list, the built-ins first and every other app after them", () => {
    mount({});
    const tiles = Array.from(root.querySelectorAll<HTMLElement>(".new-tab-launcher-tiles [data-launch]"));
    expect(tiles.map((el) => el.dataset.launch)).toEqual([
      "chat:new",
      "files:new",
      "browser:new",
      "terminal:new",
      "notes:new",
    ]);
  });

  it("rests on the project's own table and keeps the machine for search", () => {
    const attrs = mount({});
    expect(sectionKeys()).toEqual(["in-project"]);
    root.querySelector<HTMLElement>('[data-address="app:chat?instance=c1"]')!.click();
    expect(attrs.onOpenRow).toHaveBeenCalledWith(expect.objectContaining({ address: "app:chat?instance=c1" }));
    expect(root.querySelector('[data-address="app:files"]')).toBeNull();
  });

  it("drops the project table entirely when the project holds nothing", () => {
    mount({ memberRows: [] });
    expect(sectionKeys()).toEqual([]);
    expect(root.textContent).not.toContain("Nothing is in this project yet.");
    expect(root.querySelector(".new-tab-start-something")).not.toBeNull();
  });

  it("gives Everything the whole machine at rest", () => {
    mount({ isEverything: true });
    expect(sectionKeys()).toEqual(["on-machine"]);
    expect(root.querySelectorAll(".new-tab-launcher-row").length).toBe(3);
  });

  it("stands the tiles down while a create is in flight", () => {
    const attrs = mount({ isAwaitingCreate: true });
    expect(root.textContent).toContain("Starting…");
    root.querySelector<HTMLElement>('[data-launch="terminal:new"]')!.click();
    expect(attrs.onRunAction).not.toHaveBeenCalled();
  });

  it("closes the filter menu from its own toggle", () => {
    mount({ isEverything: true });
    const toggle = root.querySelector<HTMLElement>('[data-section="on-machine"] button[aria-expanded]')!;
    toggle.click();
    m.redraw.sync();
    expect(root.querySelector('input[type="checkbox"]')).not.toBeNull();
    // A real press is a pointerdown (which the open menu listens for on the document) then a click.
    toggle.dispatchEvent(new Event("pointerdown", { bubbles: true }));
    toggle.click();
    m.redraw.sync();
    expect(root.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it("filters one table by app without touching the other", () => {
    mount({ isEverything: true });
    root.querySelector<HTMLElement>('[data-section="on-machine"] button[aria-expanded]')!.click();
    m.redraw.sync();
    const checkbox = Array.from(root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))[0];
    checkbox.dispatchEvent(new Event("change"));
    m.redraw.sync();
    expect(root.querySelectorAll(".new-tab-launcher-row").length).toBe(2);
  });

  it("filters a search's machine table by app, its action rows included", () => {
    mount({});
    type("t");
    const machine = root.querySelector<HTMLElement>('[data-section="on-machine"]')!;
    expect(machine.querySelector('[data-launch="terminal:new"]')).not.toBeNull();
    uncheckAppInFilter("on-machine", "Terminal");
    expect(root.querySelector('[data-launch="terminal:new"]')).toBeNull();
    expect(root.querySelector('[data-address="app:terminal?instance=t1"]')).toBeNull();
    expect(root.querySelector('[data-launch="chat:new"]')).not.toBeNull();
    expect(root.querySelector('[data-address="app:chat?instance=c1"]')).not.toBeNull();
  });

  it("lets the filter uncheck an app the machine table shows only as an action row", () => {
    mount({});
    type("notes");
    const machine = root.querySelector<HTMLElement>('[data-section="on-machine"]')!;
    expect(machine.querySelector('[data-launch="notes:new"]')).not.toBeNull();
    expect(machine.querySelectorAll(".new-tab-launcher-row").length).toBe(1);
    uncheckAppInFilter("on-machine", "Notes");
    expect(root.querySelector('[data-launch="notes:new"]')).toBeNull();
    expect(root.querySelector('[data-section="on-machine"]')!.textContent).toContain("No tabs match this filter.");
  });

  it("starts a seeded chat from a Start something tile", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-start="build-app"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "chat" }), "new", {
      message: expect.stringContaining("build a new app"),
    });
  });

  it("disables the prompt tiles when no app takes a first message", () => {
    const attrs = mount({ tiles: [tile("terminal")] });
    const buildTile = root.querySelector<HTMLElement>('[data-start="build-app"]')!;
    expect(buildTile.getAttribute("aria-disabled")).toBe("true");
    buildTile.click();
    expect(attrs.onRunAction).not.toHaveBeenCalled();
  });

  it("scrolls to the templates from the Start from a template tile, even when picked from search", () => {
    // jsdom has no scrollIntoView; the stub lives only as long as this test.
    const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    try {
      mount({});
      type("template");
      expect(root.querySelector(".new-tab-templates")).toBeNull();
      root.querySelector<HTMLElement>('[data-start="template"]')!.click();
      m.redraw.sync();
      expect(sectionKeys()).toEqual(["in-project"]);
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.instances[0]).toBe(root.querySelector(".new-tab-templates"));
      m.redraw.sync();
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    } finally {
      HTMLElement.prototype.scrollIntoView = originalScrollIntoView;
    }
  });

  it("disables the Start from a template tile when no catalog is offered", () => {
    mount({ catalog: { kind: "disabled" } });
    const templateTile = root.querySelector<HTMLElement>('[data-start="template"]')!;
    expect(templateTile.getAttribute("aria-disabled")).toBe("true");
    expect(root.querySelector<HTMLElement>('[data-start="build-app"]')!.getAttribute("aria-disabled")).toBeNull();
    // A tile that cannot be picked does not answer the pointer with the lift.
    expect(templateTile.className).not.toContain(HOVER_LIFT_TRANSITION);
    expect(root.querySelector<HTMLElement>('[data-start="build-app"]')!.className).toContain(HOVER_LIFT_TRANSITION);
  });

  it("floats a tile without moving it, and grows its glyph instead", () => {
    mount({});
    const buildTile = root.querySelector<HTMLElement>('[data-start="build-app"]')!;
    const glyph = buildTile.querySelector<HTMLElement>("span")!;
    // The tile takes the shadow and nothing else -- its text stays put under the pointer.
    expect(buildTile.className).toContain("hover:shadow-overlay");
    expect(buildTile.className).not.toContain("scale-");
    expect(buildTile.classList).toContain("group");
    // The glyph is the one thing that grows, and it is the tile that drives it.
    expect(glyph.className).toContain("group-hover:scale-[1.15]");
    // The lift is the whole answer: a tile no longer fills behind it.
    expect(buildTile.className).not.toContain("hover:bg-fill-hover");
  });

  it("times the tile, its glyph and a template's drawing alike", () => {
    mount({});
    // One shared transition across all three, so the page's answers cannot drift apart.
    const buildTile = root.querySelector<HTMLElement>('[data-start="build-app"]')!;
    expect(buildTile.className).toContain(HOVER_LIFT_TRANSITION);
    expect(buildTile.querySelector<HTMLElement>("span")!.className).toContain(HOVER_LIFT_TRANSITION);
    const art = root.querySelector<HTMLElement>("[data-template] .new-tab-template-art")!;
    expect(art.className).toContain(HOVER_LIFT_TRANSITION);
    // A card's drawing still grows, driven by the button around it.
    expect(art.className).toContain("group-hover:scale-[1.02]");
    expect(root.querySelector<HTMLElement>("[data-template]")!.classList).toContain("group");
  });

  it("reveals the intents a page at a time behind See more, until every one is shown", () => {
    mount({});
    expect(root.querySelectorAll(".new-tab-start-tile").length).toBe(START_PAGE_SIZE);
    expect(root.querySelector('[data-start="learn"]')).toBeNull();
    root.querySelector<HTMLElement>(".new-tab-start-more")!.click();
    m.redraw.sync();
    expect(root.querySelectorAll(".new-tab-start-tile").length).toBe(START_OPTIONS.length);
    expect(root.querySelector('[data-start="learn"]')).not.toBeNull();
    expect(root.querySelector(".new-tab-start-more")).toBeNull();
  });

  it("lays the catalog out as its shelves and an All templates row, dropping a slug it does not carry", () => {
    mount({});
    const shelves = Array.from(root.querySelectorAll<HTMLElement>("[data-shelf]")).map((el) => el.dataset.shelf);
    expect(shelves).toEqual(["popular", "work", "all"]);
    const work = root.querySelector<HTMLElement>('[data-shelf="work"]')!;
    expect(Array.from(work.querySelectorAll<HTMLElement>("[data-template]")).map((el) => el.dataset.template)).toEqual(
      ["orchard"],
    );
    expect(root.querySelectorAll('[data-shelf="all"] [data-template]').length).toBe(3);
  });

  it("says when the templates are still loading, failed, or not offered", () => {
    mount({ catalog: { kind: "loading" } });
    expect(root.querySelector(".new-tab-templates-status")!.textContent).toBe("Loading templates…");
    m.mount(root, null);
    mount({ catalog: { kind: "failed" } });
    expect(root.querySelector(".new-tab-templates-status")!.textContent).toBe("Failed to load templates.");
    m.mount(root, null);
    mount({ catalog: { kind: "disabled" } });
    expect(root.querySelector(".new-tab-templates")).toBeNull();
  });

  it("opens a template's detail and adopts it into a seeded chat", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-shelf="popular"] [data-template="inbox-digest"]')!.click();
    m.redraw.sync();
    const detail = document.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(detail).not.toBeNull();
    expect(document.querySelector(".modal-card")!.textContent).toContain("Inbox-digest");
    document.querySelector<HTMLElement>(".new-tab-template-adopt")!.click();
    m.redraw.sync();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "chat" }), "new", {
      message: "/use-template https://github.com/someone/inbox-digest",
    });
    expect(document.querySelector(".new-tab-template-detail")).toBeNull();
  });

  it("asks for a new machine from the detail's other action", () => {
    const attrs = mount({});
    root.querySelector<HTMLElement>('[data-template="orchard"]')!.click();
    m.redraw.sync();
    document.querySelector<HTMLElement>(".new-tab-template-create-machine")!.click();
    m.redraw.sync();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "chat" }), "new", {
      message: expect.stringContaining("https://github.com/someone/orchard"),
    });
  });

  it("stands the detail's actions down when no app takes a first message, and keeps the dialog open", () => {
    const attrs = mount({ tiles: [tile("terminal")] });
    root.querySelector<HTMLElement>('[data-template="orchard"]')!.click();
    m.redraw.sync();
    const adopt = document.querySelector<HTMLElement>(".new-tab-template-adopt")!;
    const createMachine = document.querySelector<HTMLElement>(".new-tab-template-create-machine")!;
    expect(adopt.getAttribute("aria-disabled")).toBe("true");
    expect(createMachine.getAttribute("aria-disabled")).toBe("true");
    adopt.click();
    createMachine.click();
    m.redraw.sync();
    expect(attrs.onRunAction).not.toHaveBeenCalled();
    expect(document.querySelector(".new-tab-template-detail")).not.toBeNull();
  });

  it("swaps the page for search results: the machine, the intents, and the templates that match", () => {
    const attrs = mount({});
    type("term");
    expect(sectionKeys()).toEqual(["on-machine"]);
    // The action to open a new terminal leads, then the terminal that exists.
    const machine = root.querySelector<HTMLElement>('[data-section="on-machine"]')!;
    expect(machine.querySelector('[data-launch="terminal:new"]')).not.toBeNull();
    expect(machine.querySelector('[data-address="app:terminal?instance=t1"]')).not.toBeNull();
    expect(machine.querySelector('[data-address="app:chat?instance=c1"]')).toBeNull();
    expect(root.querySelector(".new-tab-start-tile")).toBeNull();
    expect(root.querySelector("[data-template]")).toBeNull();
    machine.querySelector<HTMLElement>('[data-launch="terminal:new"]')!.click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "terminal" }), "new", {});

    type("radar");
    expect(sectionKeys()).toEqual([]);
    expect(Array.from(root.querySelectorAll<HTMLElement>("[data-template]")).map((el) => el.dataset.template)).toEqual(
      ["weekend-radar"],
    );

    type("routine");
    expect(root.querySelectorAll(".new-tab-start-tile").length).toBe(1);
    expect(root.querySelector('[data-start="routine"]')).not.toBeNull();

    type("zzzz nothing");
    expect(root.querySelector(".new-tab-launcher-no-matches")!.textContent).toContain("zzzz nothing");
  });

  it("clears the search from its button and puts the resting page back", () => {
    mount({});
    type("term");
    expect(root.querySelector(".new-tab-start-something")).toBeNull();
    root.querySelector<HTMLElement>(".new-tab-launcher-search-clear")!.click();
    m.redraw.sync();
    expect(root.querySelector(".new-tab-start-something")).not.toBeNull();
    expect(sectionKeys()).toEqual(["in-project"]);
  });
});
