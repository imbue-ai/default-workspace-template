// @vitest-environment jsdom
//
// The pure helpers below never touch the DOM and would run fine under
// vitest's node default, but the switcher-rendering suite further down mounts
// the actual component and needs real DOM APIs (event dispatch, document.body,
// getBoundingClientRect) to do it -- jsdom is the whole file's environment
// rather than one describe block's, since Vitest only reads the environment
// pragma once, for the file.
import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. jsdom provides one, so this is a no-op here; it stays so the pure
// helpers above would still work if this file ever moved back to node.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The rail reaches the live workspace only through the app list. Most tests
// never consult it (empty by default); the pin-icon suite below overrides the
// mock's return value per test to exercise a rail carrying a pinned app.
vi.mock("../models/AgentManager", () => ({
  getApps: vi.fn(() => []),
}));

// Only the primary-agent id is faked: it is read from a meta tag the server
// injects and cached on first call, so a test cannot set it by touching the
// DOM. Everything else in the module (apiUrl, the feature flags) stays real,
// since the rail's own project calls go through it.
vi.mock("../base-path", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../base-path")>()),
  getPrimaryAgentId: vi.fn(() => ""),
}));

import m from "mithril";

import { getPrimaryAgentId } from "../base-path";
import type { AppEntry } from "../models/AgentManager";
import { getApps } from "../models/AgentManager";
import { applyMemberTitleChange } from "../models/MemberTitles";
import type { ProjectInfo } from "../models/Projects";
import { EVERYTHING_VIEW_ID } from "../models/Projects";
import type { SidebarAttrs, SidebarTabRow } from "./Sidebar";
import { Sidebar, nextGlyphIndex, nextProjectName, pinnedAppNamesForView, placeMenu } from "./Sidebar";
import { SQUIGGLE_GLYPHS } from "./squiggles";

/** A tab-list row, built the way the workspace builds them (see
 *  `getSidebarRows`): the ref carries the kind, and nothing else matters here. */
function row(ref: string, kind: SidebarTabRow["kind"]): SidebarTabRow {
  return { ref, kind, label: ref, isOpen: false };
}

const VIEWPORT = { width: 1000, height: 800 };

describe("placeMenu", () => {
  it("hangs a 'below' menu off the anchor's bottom-left", () => {
    const anchor = { left: 40, right: 280, top: 100, bottom: 134, width: 240 };
    expect(placeMenu(anchor, { width: 240, height: 200 }, VIEWPORT, "below")).toEqual({ left: 40, top: 134 });
  });

  it("flips a 'below' menu above its anchor when it would run off the bottom", () => {
    const anchor = { left: 40, right: 280, top: 600, bottom: 640, width: 240 };
    expect(placeMenu(anchor, { width: 240, height: 300 }, VIEWPORT, "below")).toEqual({ left: 40, top: 300 });
  });

  it("puts a 'right' menu beside its anchor's top-right", () => {
    const anchor = { left: 100, right: 140, top: 200, bottom: 228, width: 40 };
    expect(placeMenu(anchor, { width: 180, height: 120 }, VIEWPORT, "right")).toEqual({ left: 140, top: 200 });
  });

  it("flips a 'right' menu to the left of its anchor when it would run off the edge", () => {
    const anchor = { left: 900, right: 940, top: 200, bottom: 228, width: 40 };
    expect(placeMenu(anchor, { width: 180, height: 120 }, VIEWPORT, "right")).toEqual({ left: 720, top: 200 });
  });

  it("clamps to the viewport when neither side fits", () => {
    const anchor = { left: 990, right: 998, top: 780, bottom: 796, width: 8 };
    // Too wide to flip (the left side would start at -2), so it clamps instead.
    expect(placeMenu(anchor, { width: 992, height: 400 }, VIEWPORT, "right")).toEqual({ left: 6, top: 394 });
  });
});

describe("pinnedAppNamesForView", () => {
  const ROWS = [
    row("chat:agent-1", "chat"),
    row("service:grafana", "app"),
    row("terminal:build", "terminal"),
    row("service:browser?session=2", "browser"),
    row("service:docs", "app"),
    row("url:abc123", "url"),
  ];

  it("takes the app members, in member order", () => {
    // Member order, not alphabetical: the rail's shortcuts read in the order
    // the apps were pinned.
    expect(pinnedAppNamesForView(ROWS, false)).toEqual(["grafana", "docs"]);
  });

  it("pins nothing in Everything", () => {
    // The unfiltered view lists every app on the machine already, so there is
    // nothing for it to shortcut.
    expect(pinnedAppNamesForView(ROWS, true)).toEqual([]);
  });

  it("is empty for a view holding no apps", () => {
    expect(pinnedAppNamesForView([row("chat:agent-1", "chat")], false)).toEqual([]);
  });
});

describe("nextProjectName", () => {
  const project = (name: string, projectId?: string) => ({
    name,
    // The id the server would have minted for the name, unless the test says
    // otherwise -- which is exactly the renamed-project case below.
    project_id:
      projectId ??
      name
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-"),
  });

  it("starts at one on an empty machine", () => {
    expect(nextProjectName([])).toBe("Project 1");
  });

  it("skips the numbers already taken, whatever their casing", () => {
    expect(nextProjectName([project("project 1"), project("Newsreader"), project("PROJECT 2")])).toBe("Project 3");
  });

  it("fills a gap rather than counting past it", () => {
    expect(nextProjectName([project("Project 1"), project("Project 3")])).toBe("Project 2");
  });

  it("skips an id a renamed project still owns", () => {
    // A rename keeps the id, so the starter project renamed to "Something"
    // still holds project-1. Proposing "Project 1" would bounce off the id
    // conflict at create time; the mint has to step past it.
    expect(nextProjectName([project("Something", "project-1")])).toBe("Project 2");
  });
});

describe("nextGlyphIndex", () => {
  it("takes the first unused glyph", () => {
    expect(nextGlyphIndex([0, 1, 3])).toBe(2);
  });

  it("starts repeating once every glyph is in use", () => {
    const allGlyphs = SQUIGGLE_GLYPHS.map((_glyph, index) => index);
    expect(nextGlyphIndex(allGlyphs)).toBe(0);
  });
});

// ---------- Rendering: the switcher dropdown and the rail's tooltips ----------

const PROJECT_A: ProjectInfo = {
  project_id: "project-a",
  name: "Alpha",
  color: "#c0392b",
  glyph: 0,
  has_content: true,
  members: [],
};
const PROJECT_B: ProjectInfo = {
  project_id: "project-b",
  name: "Beta",
  color: "#2980b9",
  glyph: 1,
  has_content: true,
  members: [],
};

function makeAttrs(overrides: Partial<SidebarAttrs> = {}): SidebarAttrs {
  return {
    projects: [PROJECT_A, PROJECT_B],
    activeViewId: PROJECT_A.project_id,
    rows: [],
    onSelectView: vi.fn(),
    onProjectsChanged: vi.fn(),
    onProjectCreated: vi.fn(),
    onOpenTabType: vi.fn(),
    onOpenApp: vi.fn(),
    onSetAppPinned: vi.fn(),
    onSetShortcutPinned: vi.fn(),
    onSetShortcutMode: vi.fn(),
    onNewOfKind: vi.fn(),
    onFocusLastOfKind: vi.fn(),
    awaitingShortcutIds: new Set<string>(),
    onOpenRow: vi.fn(),
    onRefreshRow: vi.fn(),
    onRenameRow: vi.fn(),
    onRemoveFromView: vi.fn(),
    onShareApp: vi.fn(),
    historyActionForService: vi.fn(() => null),
    systemHistoryAction: vi.fn(() => null),
    onAddRowToProjects: vi.fn(),
    onStopRow: vi.fn(),
    onServiceLifecycle: vi.fn(),
    onDeleteFromMachine: vi.fn(),
    ...overrides,
  };
}

/** Mounts a fresh Sidebar instance into a detached root and returns a
 *  `redraw` the test calls after simulating an event. Driven with plain
 *  `m.render` rather than `m.mount`'s RAF-scheduled auto-redraw, so every
 *  state change the component's own closures make lands synchronously and no
 *  test has to race a timer for it. */
function mountSidebar(attrs: SidebarAttrs): { root: HTMLElement; redraw: () => void } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const component = Sidebar();
  const redraw = (): void => {
    m.render(root, m(component, attrs));
  };
  redraw();
  return { root, redraw };
}

function click(element: Element | null): void {
  if (element === null) throw new Error("nothing to click");
  element.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

/** The switcher's row for this label -- the row's own click target, not the
 *  edit pencil nested inside it. */
function switcherRow(root: HTMLElement, label: string): HTMLElement {
  const rows = Array.from(root.querySelectorAll(".project-rail-menu-item"));
  const found = rows.find((candidate) => candidate.textContent?.includes(label));
  if (found === undefined) throw new Error(`no switcher row for "${label}"`);
  return found as HTMLElement;
}

describe("Sidebar switcher dropdown", () => {
  it("opens on a header click and gives every project row its own rename pencil", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(root.querySelector('[aria-label="Edit Alpha"]')).not.toBeNull();
    expect(root.querySelector('[aria-label="Edit Beta"]')).not.toBeNull();
  });

  it("marks only the current project's row with a checkmark, not a background fill", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ activeViewId: PROJECT_A.project_id }));
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(switcherRow(root, "Alpha").querySelector(".project-rail-check")).not.toBeNull();
    expect(switcherRow(root, "Beta").querySelector(".project-rail-check")).toBeNull();
    expect(switcherRow(root, "Alpha").className).not.toContain("bg-bg-sidebar");
    // The active row's pencil is still there (revealed on hover, swapping
    // places with the checkmark) -- rendered in the DOM either way, since the
    // swap is pure CSS opacity, not conditional rendering.
    expect(switcherRow(root, "Alpha").querySelector('[aria-label="Edit Alpha"]')).not.toBeNull();
  });

  it("marks Everything the same way when it is the active view, but gives it no rename pencil", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ activeViewId: EVERYTHING_VIEW_ID }));
    click(root.querySelector(".project-rail-header"));
    redraw();
    const everythingRow = switcherRow(root, "Everything");
    expect(everythingRow.querySelector(".project-rail-check")).not.toBeNull();
    expect(everythingRow.className).not.toContain("bg-bg-sidebar");
    expect(everythingRow.querySelector('[aria-label^="Edit"]')).toBeNull();
  });

  it("switches to a non-current row on a plain click", () => {
    const attrs = makeAttrs({ activeViewId: PROJECT_A.project_id });
    const { root, redraw } = mountSidebar(attrs);
    click(root.querySelector(".project-rail-header"));
    redraw();
    click(switcherRow(root, "Beta"));
    expect(attrs.onSelectView).toHaveBeenCalledWith(PROJECT_B.project_id);
  });

  it("does nothing when the current row is clicked outside its pencil", () => {
    const attrs = makeAttrs({ activeViewId: PROJECT_A.project_id });
    const { root, redraw } = mountSidebar(attrs);
    click(root.querySelector(".project-rail-header"));
    redraw();
    click(switcherRow(root, "Alpha"));
    expect(attrs.onSelectView).not.toHaveBeenCalled();
  });

  it("opens a non-current row's own settings from its pencil, without switching to it", () => {
    const attrs = makeAttrs({ activeViewId: PROJECT_A.project_id });
    const { root, redraw } = mountSidebar(attrs);
    click(root.querySelector(".project-rail-header"));
    redraw();
    click(root.querySelector('[aria-label="Edit Beta"]'));
    redraw();
    expect(attrs.onSelectView).not.toHaveBeenCalled();
    const nameField = root.querySelector("input.custom-url-dialog-input") as HTMLInputElement | null;
    expect(nameField?.value).toBe("Beta");
  });

  it("offers System history below the views, and runs it", () => {
    const ran: string[] = [];
    const attrs = makeAttrs({ systemHistoryAction: vi.fn(() => () => ran.push("system")) });
    const { root, redraw } = mountSidebar(attrs);
    click(root.querySelector(".project-rail-header"));
    redraw();

    click(switcherRow(root, "System history"));
    expect(ran).toEqual(["system"]);
    expect(attrs.onSelectView).not.toHaveBeenCalled();
  });

  it("draws no System history row where there is no timeline to open", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ systemHistoryAction: vi.fn(() => null) }));
    click(root.querySelector(".project-rail-header"));
    redraw();

    const labels = Array.from(root.querySelectorAll(".project-rail-menu-item")).map((element) => element.textContent);
    expect(labels).not.toContain("System history");
  });

  it("sizes the dropdown to its own fixed width rather than the header's -- a touch wider than the rail", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    const menu = root.querySelector('.project-rail-menu[role="menu"]') as HTMLElement | null;
    expect(menu?.style.width).toBe("256px");
  });

  it("collapses the rail on a completed view switch, even with no mouseleave", () => {
    // Regression: switching views rebuilds the rail's own DOM subtree, so a
    // pointer already resting on it picks up a fresh native mouseenter with
    // no mouseleave to follow -- nothing native is left to flip `expanded`
    // back once the pointer actually does leave later. A completed switch is
    // itself strong evidence the interaction is over, so it collapses the
    // rail directly rather than depending solely on hover. Driven with raw
    // `m.render` rather than `mountSidebar`, since this needs a SECOND render
    // with a genuinely different `activeViewId`, not a re-render of the same
    // attrs `mountSidebar`'s closure captured.
    const root = document.createElement("div");
    document.body.appendChild(root);
    const component = Sidebar();
    const render = (activeViewId: string): void => {
      m.render(root, m(component, makeAttrs({ activeViewId })));
    };

    render(PROJECT_A.project_id);
    // The rail's own top-level element carries the hover handlers.
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    render(PROJECT_A.project_id);
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    render(PROJECT_B.project_id);
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });

  it("folds up when the pointer leaves the window without a leave reaching the rail", () => {
    // The slot's own mouseleave handles every crossing inside the page, but a
    // cursor exiting the WINDOW does not reliably produce one (Electron, which
    // is what minds is, can let it out silently) -- and the rail was then left
    // expanded over the dock with the pointer on another app. A `mouseout`
    // naming no related target is what says "gone from this document".
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    document.dispatchEvent(new MouseEvent("mouseout", { relatedTarget: null, bubbles: true }));
    redraw();
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });

  it("folds up when the window loses focus, not just its menus", () => {
    // The UI is a cross-origin iframe inside the minds chrome, so a cursor
    // leaving the outer window may raise no boundary event in here at all.
    // Blur is the one signal that always arrives for the click-away case, and
    // a rail left expanded over the dock while the user is in another app is
    // the same wrong as a menu left there.
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    window.dispatchEvent(new Event("blur"));
    redraw();
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });

  it("folds up on a document-level pointer leave", () => {
    // The companion to the `mouseout` path: registered as well as, not instead
    // of, since neither is reliable on its own inside an embedded frame.
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    document.documentElement.dispatchEvent(new MouseEvent("mouseleave"));
    redraw();
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });

  it("stays open when the pointer merely moves between elements in the page", () => {
    // Every ordinary crossing names the element being entered, including into
    // an iframe pane; only a true exit from the document names nothing. Acting
    // on those would collapse the rail while the pointer is still on it.
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    document.dispatchEvent(
      new MouseEvent("mouseout", { relatedTarget: document.createElement("iframe"), bubbles: true }),
    );
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();
  });

  it("leaves an open menu alone when the pointer leaves the window", () => {
    // The menu is the thing the user is working and extends past the rail, so
    // it holds the rail open exactly as it does for the slot's own leave.
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).not.toBeNull();

    document.dispatchEvent(new MouseEvent("mouseout", { relatedTarget: null, bubbles: true }));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).not.toBeNull();
  });

  it("goes primary on hover for the tertiary 'New project' row", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    const newProjectRow = switcherRow(root, "New project");
    expect(newProjectRow.className).toContain("text-text-faint");
    expect(newProjectRow.className).toContain("hover:text-text-primary");
  });
});

describe("Sidebar menus hold the rail open", () => {
  it("keeps the rail expanded through a mouseleave while any menu is open, not just All apps", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).not.toBeNull();

    // Reaching the menu means the pointer has to leave the 37-240px rail box
    // first -- that must not fold the rail (and the switcher hanging off it)
    // back up from under the pointer.
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseleave"));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).not.toBeNull();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();
  });

  it("still collapses on a mouseleave once nothing is open", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseleave"));
    redraw();
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });
});

describe("Sidebar menu scrim", () => {
  it("renders no scrim when nothing is open", () => {
    const { root } = mountSidebar(makeAttrs());
    expect(root.querySelector(".fixed.inset-0.z-40")).toBeNull();
  });

  it("renders a scrim behind an open menu, and a press on it closes the menu and collapses the rail", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    click(root.querySelector(".project-rail-header"));
    redraw();
    const scrim = root.querySelector(".fixed.inset-0.z-40");
    expect(scrim).not.toBeNull();

    scrim?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
    redraw();
    // The scrim itself closed the menu (rather than the press falling through
    // to whatever sits underneath it) -- a definitive dismissal, so the rail
    // folds up with it just as an outside click or Escape already did.
    expect(root.querySelector('.project-rail-menu[role="menu"]')).toBeNull();
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });
});

describe("Sidebar row clicks", () => {
  it("collapses the rail when the clicked row is already open, and leaves it expanded otherwise", () => {
    const rows: SidebarTabRow[] = [
      { ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true },
      { ref: "chat:agent-2", kind: "chat", label: "Chat 2", isOpen: false },
    ];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    const closedRow = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes("Chat 2"),
    );
    click(closedRow ?? null);
    redraw();
    expect(attrs.onOpenRow).toHaveBeenCalledWith(rows[1]);
    // Not already open: focusing it is a real, visible change, so the rail
    // stays put the way it always has.
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    const openRow = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes("Chat 1"),
    );
    click(openRow ?? null);
    redraw();
    expect(attrs.onOpenRow).toHaveBeenCalledWith(rows[0]);
    // Already open: the click could not have changed anything on screen, so
    // it would otherwise look like it did nothing -- collapse the rail
    // instead of leaving it sitting over the tab the click just focused.
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });
});

describe("Sidebar stopped tab-list rows", () => {
  it("dims a tab-list row carrying a stopped detail", () => {
    const attrs = makeAttrs({
      rows: [
        { ref: "service:docs", kind: "app", label: "docs", isOpen: true, stoppedDetail: "stopped" },
        { ref: "service:web", kind: "app", label: "web", isOpen: true },
      ],
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const stoppedRow = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes("docs"),
    );
    const runningRow = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes("web"),
    );
    expect(stoppedRow?.classList.contains("project-rail-tab-stopped")).toBe(true);
    expect(runningRow?.classList.contains("project-rail-tab-stopped")).toBe(false);
  });
});

describe("Sidebar pinned-app rows", () => {
  const DEMO_APP: AppEntry = { name: "grafana", url: "http://example.test", label: "grafana-abc123" };

  it("unpins a pinned app in one click, without opening it", () => {
    vi.mocked(getApps).mockReturnValue([DEMO_APP]);
    try {
      const attrs = makeAttrs({
        rows: [{ ref: "service:grafana", kind: "app", label: "grafana", isOpen: false }],
      });
      const { root, redraw } = mountSidebar(attrs);
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      const pinButton = root.querySelector(".project-rail-pin");
      expect(pinButton).not.toBeNull();
      click(pinButton);
      redraw();

      expect(attrs.onSetAppPinned).toHaveBeenCalledWith(DEMO_APP, false);
      expect(attrs.onOpenApp).not.toHaveBeenCalled();
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("shows no pin toggle while the rail is collapsed", () => {
    vi.mocked(getApps).mockReturnValue([DEMO_APP]);
    try {
      const attrs = makeAttrs({
        rows: [{ ref: "service:grafana", kind: "app", label: "grafana", isOpen: false }],
      });
      const { root } = mountSidebar(attrs);
      expect(root.querySelector(".project-rail-pin")).toBeNull();
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("dims a pinned app whose backing service is stopped", () => {
    vi.mocked(getApps).mockReturnValue([{ ...DEMO_APP, program: "grafana", is_running: false }]);
    try {
      const attrs = makeAttrs({
        rows: [{ ref: "service:grafana", kind: "app", label: "grafana", isOpen: false }],
      });
      const { root, redraw } = mountSidebar(attrs);
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      expect(root.querySelector(".project-rail-shortcut-stopped")).not.toBeNull();
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("renders a running pinned app undimmed", () => {
    vi.mocked(getApps).mockReturnValue([{ ...DEMO_APP, program: "grafana", is_running: true }]);
    try {
      const attrs = makeAttrs({
        rows: [{ ref: "service:grafana", kind: "app", label: "grafana", isOpen: false }],
      });
      const { root, redraw } = mountSidebar(attrs);
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      expect(root.querySelector(".project-rail-shortcut-stopped")).toBeNull();
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("calls a renamed app what the user named it, the unpin button included", () => {
    // The shortcut and the tab list are two views of one object, so a rename
    // has to reach the row's own control too -- announcing the registration
    // name there would leave the row and its button disagreeing.
    vi.mocked(getApps).mockReturnValue([DEMO_APP]);
    applyMemberTitleChange("service:grafana", "Dashboards");
    try {
      const attrs = makeAttrs({
        rows: [{ ref: "service:grafana", kind: "app", label: "Dashboards", isOpen: false }],
      });
      const { root, redraw } = mountSidebar(attrs);
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      const shortcut = root.querySelector(".project-rail-shortcut.group");
      expect(shortcut?.textContent).toContain("Dashboards");
      expect(root.querySelector('[aria-label="Unpin Dashboards"]')).not.toBeNull();
    } finally {
      applyMemberTitleChange("service:grafana", null);
      vi.mocked(getApps).mockReturnValue([]);
    }
  });
});

describe("Sidebar row menu (shared object-menu entries)", () => {
  /** Right-clicks the given rail row's own text and returns the menu that
   *  opens, if any -- the rail's row menu, not the switcher or All apps. */
  function openRowMenuByContextClick(root: HTMLElement, redraw: () => void, label: string): HTMLElement | null {
    const target = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes(label),
    );
    target?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    redraw();
    return root.querySelector('.project-rail-menu[role="menu"]');
  }

  function menuItemLabels(menu: HTMLElement | null): (string | null)[] {
    return Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).map((element) => element.textContent);
  }

  it("opens on a right-click, the same as the kebab button does", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).toBeNull();

    const menu = openRowMenuByContextClick(root, redraw, "Chat 1");
    expect(menu).not.toBeNull();
  });

  it("renders the shared verb set for an open chat row, not the old removal items", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openRowMenuByContextClick(root, redraw, "Chat 1");
    // The acting group, then the removal group -- exactly
    // objectMenuEntries("chat", ...) as the rail builds it. No Share, which is
    // an app-only affordance, and no "Delete from this machine", which folded
    // into the shared Delete verb.
    expect(menuItemLabels(menu)).toEqual([
      "Refresh",
      "Add to project...",
      "Rename",
      "Remove from project",
      "Stop Chat 1",
      "Delete Chat 1",
    ]);
    expect(menu?.textContent).not.toContain("Delete from this machine");
    // Closing a tab is the tab's own job; from here the row is a thing the
    // project shows, so the verb that belongs to it is taking it out.
    expect(menu?.textContent).not.toContain("Close tab");
  });

  it("withholds Stop and Delete from the primary agent's own chat, as the tab menu does", () => {
    // That agent runs the workspace's services, so stopping or deleting it
    // would take the machine down with it. The tab's build has always withheld
    // the verbs; the rail renders the same shared set, so it has to withhold
    // them too -- and by id, since a chat can be renamed to anything.
    vi.mocked(getPrimaryAgentId).mockReturnValue("agent-primary");
    try {
      const rows: SidebarTabRow[] = [
        { ref: "chat:agent-primary", kind: "chat", label: "Chat 1", isOpen: true },
        { ref: "chat:agent-2", kind: "chat", label: "Chat 2", isOpen: true },
      ];
      const { root, redraw } = mountSidebar(makeAttrs({ rows }));
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "Chat 1"))).toEqual([
        "Refresh",
        "Add to project...",
        "Rename",
        "Remove from project",
      ]);
    } finally {
      vi.mocked(getPrimaryAgentId).mockReturnValue("");
    }
  });

  it("still offers Delete on any other chat row", () => {
    vi.mocked(getPrimaryAgentId).mockReturnValue("agent-primary");
    try {
      const rows: SidebarTabRow[] = [{ ref: "chat:agent-2", kind: "chat", label: "Chat 2", isOpen: true }];
      const { root, redraw } = mountSidebar(makeAttrs({ rows }));
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "Chat 2"))).toContain("Delete Chat 2");
    } finally {
      vi.mocked(getPrimaryAgentId).mockReturnValue("");
    }
  });

  it("offers History on an app row, keyed by the service rather than the instance", () => {
    const attrs = makeAttrs({
      rows: [{ ref: "service:curio?instance=curio-2", kind: "app", label: "Curio 2", isOpen: true }],
      historyActionForService: vi.fn((serviceName: string) => (serviceName === "curio" ? () => undefined : null)),
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "Curio 2"))).toContain("History");
    expect(attrs.historyActionForService).toHaveBeenCalledWith("curio");
  });

  it("offers no History on a row whose app has no timeline, nor on any other kind", () => {
    const attrs = makeAttrs({
      rows: [
        { ref: "service:curio?instance=curio-2", kind: "app", label: "Curio 2", isOpen: true },
        { ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true },
      ],
      historyActionForService: vi.fn(() => null),
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "Curio 2"))).not.toContain("History");
    expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "Chat 1"))).not.toContain("History");
    expect(attrs.historyActionForService).not.toHaveBeenCalledWith("agent-1");
  });

  it("leaves a History row exactly Refresh and Remove from project", () => {
    const attrs = makeAttrs({
      rows: [{ ref: "service:versioning?instance=versioning-1", kind: "app", label: "History", isOpen: true }],
      historyActionForService: vi.fn(() => () => undefined),
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "History"))).toEqual([
      "Refresh",
      "Remove from project",
    ]);
  });

  it("leaves a History row in Everything just Refresh", () => {
    const attrs = makeAttrs({
      activeViewId: EVERYTHING_VIEW_ID,
      rows: [{ ref: "service:versioning?instance=versioning-1", kind: "app", label: "History", isOpen: true }],
      historyActionForService: vi.fn(() => () => undefined),
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuItemLabels(openRowMenuByContextClick(root, redraw, "History"))).toEqual(["Refresh"]);
  });

  it("shows no one-click remove on a row whose menu carries the verb", () => {
    const attrs = makeAttrs({ rows: [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }] });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(root.querySelector(".project-rail-remove")).toBeNull();
  });

  it("keeps the one-click remove on a menu-less legacy url row, without opening it", () => {
    const attrs = makeAttrs({ rows: [{ ref: "url:abc123", kind: "url", label: "Some Page", isOpen: false }] });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    click(root.querySelector(".project-rail-remove"));
    expect(attrs.onRemoveFromView).toHaveBeenCalledTimes(1);
    // The row underneath opens the object; the button must not have.
    expect(attrs.onOpenRow).not.toHaveBeenCalled();
  });

  it("shows no one-click remove in Everything", () => {
    const rows: SidebarTabRow[] = [{ ref: "url:abc123", kind: "url", label: "Some Page", isOpen: false }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows, activeViewId: EVERYTHING_VIEW_ID }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(root.querySelector(".project-rail-remove")).toBeNull();
  });

  it("drops a shortcut this project unpinned, and keeps the rest", () => {
    const unpinned: ProjectInfo = { ...PROJECT_A, shortcut_overrides: { terminal: { is_pinned: false } } };
    const { root, redraw } = mountSidebar(
      makeAttrs({ projects: [unpinned, PROJECT_B], activeViewId: unpinned.project_id }),
    );
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const labels = Array.from(root.querySelectorAll(".project-rail-shortcut")).map((el) => el.textContent);
    expect(labels).not.toContain("Terminal");
    // Chat's label follows its default mode, which is new ("New Chat").
    expect(labels).toContain("New Chat");
  });

  it("shows all four until a project unpins one", () => {
    // Absence has to mean the default: every project shows the full set until
    // it says otherwise.
    const { root, redraw } = mountSidebar(makeAttrs());
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const labels = Array.from(root.querySelectorAll(".project-rail-shortcut")).map((el) => el.textContent);
    // Chat's default mode is new, so its row reads "New Chat"; the rest
    // default to focus and keep their plain labels.
    expect(labels).toEqual(["New Chat", "File Viewer", "Browser", "Terminal"]);
  });

  it("opens the File Viewer once a files app backs it", () => {
    vi.mocked(getApps).mockReturnValue([{ name: "files", url: "http://files.test", label: "files-abc123" }]);
    try {
      const attrs = makeAttrs();
      const { root, redraw } = mountSidebar(attrs);
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      const filesButton = Array.from(root.querySelectorAll("button.project-rail-shortcut")).find(
        (el) => el.textContent === "File Viewer",
      );
      expect(filesButton?.hasAttribute("disabled")).toBe(false);
      click(filesButton ?? null);
      expect(attrs.onOpenTabType).toHaveBeenCalledWith("files");
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("keeps the File Viewer disabled where no app backs it", () => {
    // A workspace built before the dufs "files" service shipped registers no
    // such app; the row stays put but must not pretend to work.
    const attrs = makeAttrs();
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const filesButton = Array.from(root.querySelectorAll("button.project-rail-shortcut")).find(
      (el) => el.textContent === "File Viewer",
    );
    expect(filesButton?.hasAttribute("disabled")).toBe(true);
    click(filesButton ?? null);
    expect(attrs.onOpenTabType).not.toHaveBeenCalled();
  });

  it("reports an unpin without starting the shortcut", () => {
    const attrs = makeAttrs();
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    click(root.querySelector('button[aria-label="Unpin Chat from this project"]'));
    expect(attrs.onSetShortcutPinned).toHaveBeenCalledWith("chat", false);
    expect(attrs.onOpenTabType).not.toHaveBeenCalled();
  });

  it("offers no unpin in Everything, which has nowhere to record one", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ activeViewId: EVERYTHING_VIEW_ID }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(root.querySelector(".project-rail-shortcut-unpin")).toBeNull();
    const labels = Array.from(root.querySelectorAll(".project-rail-shortcut")).map((el) => el.textContent);
    // Chat's default mode is new, so its row reads "New Chat"; the rest
    // default to focus and keep their plain labels.
    expect(labels).toEqual(["New Chat", "File Viewer", "Browser", "Terminal"]);
  });

  it("offers no Remove from project in Everything, which is the home", () => {
    // Everything is not a project and nothing can be taken out of it -- an
    // object leaves it only by being destroyed.
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows, activeViewId: EVERYTHING_VIEW_ID }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openRowMenuByContextClick(root, redraw, "Chat 1");
    expect(menuItemLabels(menu)).not.toContain("Remove from project");
  });

  it("routes Remove from project to onRemoveFromView", () => {
    const attrs = makeAttrs({ rows: [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }] });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openRowMenuByContextClick(root, redraw, "Chat 1");
    const item = Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).find(
      (element) => element.textContent === "Remove from project",
    );
    click(item ?? null);
    expect(attrs.onRemoveFromView).toHaveBeenCalledTimes(1);
  });

  it("gives a terminal row no Rename and no Close tab, whether or not it is open", () => {
    const rows: SidebarTabRow[] = [{ ref: "terminal:build", kind: "terminal", label: "Terminal 1", isOpen: false }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openRowMenuByContextClick(root, redraw, "Terminal 1");
    // No Rename either: a terminal IS its tmux session name, so the verb is
    // withheld rather than offered as a display name over the top.
    expect(menuItemLabels(menu)).toEqual(["Refresh", "Add to project...", "Remove from project", "Delete Terminal 1"]);
  });

  it("still lists an app the machine no longer offers, which has no shortcut row", () => {
    // The shortcut strip is built from the live app list, so a member of an
    // unregistered app is not up there. Dropping it from the list too would
    // leave it showing nowhere, and removable from nowhere.
    vi.mocked(getApps).mockReturnValue([]);
    const rows: SidebarTabRow[] = [{ ref: "service:gone", kind: "app", label: "Gone", isOpen: false }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const listed = Array.from(root.querySelectorAll(".project-rail-tab")).map((element) => element.textContent);
    expect(listed.some((text) => text?.includes("Gone"))).toBe(true);
  });

  it("keeps an app's verbs reachable from its shortcut row, which is its only row now", () => {
    // The tab list no longer repeats an app, so its shortcut row has to carry
    // the menu -- otherwise Share and Stop would exist only while the app
    // happened to have a tab open.
    vi.mocked(getApps).mockReturnValue([
      { name: "grafana", url: "http://example.test", label: "grafana-abc123", program: "grafana" },
    ]);
    try {
      const rows: SidebarTabRow[] = [{ ref: "service:grafana", kind: "app", label: "Grafana", isOpen: false }];
      const { root, redraw } = mountSidebar(makeAttrs({ rows }));
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();

      // Not in the list below...
      expect(root.querySelector(".project-rail-tab")).toBeNull();
      // ...and its menu opens from the shortcut row instead.
      // Found by the name the SHORTCUT draws, which is the app's registered
      // one until someone gives it another; the menu's own labels come from the
      // member row, which is why they read "Grafana" below.
      const shortcut = Array.from(root.querySelectorAll(".project-rail-shortcut")).find((element) =>
        element.textContent?.includes("grafana"),
      );
      shortcut?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
      redraw();

      // Every verb names the app the way the row does. The registered service
      // name ("grafana") is the app's stable id -- it keys the ref, apps.toml
      // and the supervisord program -- and the share is still keyed by it; it
      // just no longer surfaces. The destructive slot is the reversible
      // service-level Stop, not a destroy: the app is supervised (it carries a
      // program), so the workspace can honestly stop and start it.
      const menu = root.querySelector<HTMLElement>('.project-rail-menu[role="menu"]');
      // The object verbs, then (after a divider) the shortcut group: the
      // complementary "New Grafana" and the mode flip.
      expect(menuItemLabels(menu)).toEqual([
        "Refresh",
        "Share Grafana",
        "Add to project...",
        "Remove from project",
        "Stop Grafana",
        "New Grafana",
        'Change shortcut to "New Grafana"',
      ]);
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("offers Start instead of Stop for a stopped app", () => {
    vi.mocked(getApps).mockReturnValue([
      { name: "grafana", url: "http://example.test", label: "grafana-abc123", program: "grafana", is_running: false },
    ]);
    try {
      const rows: SidebarTabRow[] = [{ ref: "service:grafana", kind: "app", label: "Grafana", isOpen: false }];
      const { root, redraw } = mountSidebar(makeAttrs({ rows }));
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();
      const shortcut = Array.from(root.querySelectorAll(".project-rail-shortcut")).find((element) =>
        element.textContent?.includes("grafana"),
      );
      shortcut?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
      redraw();

      const menu = root.querySelector<HTMLElement>('.project-rail-menu[role="menu"]');
      expect(menuItemLabels(menu)).toContain("Start Grafana");
      expect(menuItemLabels(menu)).not.toContain("Stop Grafana");
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("offers no stop or start for an app without a supervised program", () => {
    // The workspace cannot honestly stop what nothing here supervises, and the
    // old deregister-flavored Quit is gone entirely: removal is the mind's job
    // via update-app.
    vi.mocked(getApps).mockReturnValue([{ name: "grafana", url: "http://example.test", label: "grafana-abc123" }]);
    try {
      const rows: SidebarTabRow[] = [{ ref: "service:grafana", kind: "app", label: "Grafana", isOpen: false }];
      const { root, redraw } = mountSidebar(makeAttrs({ rows }));
      root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
      redraw();
      const shortcut = Array.from(root.querySelectorAll(".project-rail-shortcut")).find((element) =>
        element.textContent?.includes("grafana"),
      );
      shortcut?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
      redraw();

      const menu = root.querySelector<HTMLElement>('.project-rail-menu[role="menu"]');
      expect(menuItemLabels(menu)).toEqual([
        "Refresh",
        "Share Grafana",
        "Add to project...",
        "Remove from project",
        "New Grafana",
        'Change shortcut to "New Grafana"',
      ]);
    } finally {
      vi.mocked(getApps).mockReturnValue([]);
    }
  });

  it("offers no row menu at all for a legacy url member", () => {
    const rows: SidebarTabRow[] = [{ ref: "url:abc123", kind: "url", label: "Some Page", isOpen: false }];
    const { root, redraw } = mountSidebar(makeAttrs({ rows }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const row = Array.from(root.querySelectorAll(".project-rail-tab")).find((element) =>
      element.textContent?.includes("Some Page"),
    );
    expect(row?.querySelector('[aria-label^="Actions for"]')).toBeNull();
    row?.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    redraw();
    expect(root.querySelector('.project-rail-menu[role="menu"]')).toBeNull();
  });

  it("routes Delete to onDeleteFromMachine, closing the menu", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openRowMenuByContextClick(root, redraw, "Chat 1");
    const quitItem = Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).find(
      (element) => element.textContent === "Delete Chat 1",
    );
    click(quitItem ?? null);
    redraw();
    expect(attrs.onDeleteFromMachine).toHaveBeenCalledWith(rows[0]);
    expect(root.querySelector('.project-rail-menu[role="menu"]')).toBeNull();
  });

  /** Open a row's rename field through its menu and type into it the way a
   *  browser does -- the DOM value changes and an `input` event follows -- with
   *  a redraw after, since the rail redraws constantly under a live workspace
   *  (every WebSocket event ends in one, and mithril schedules its own after
   *  each handler it binds, this field's `onkeydown` included). */
  function typeIntoRenameField(
    root: HTMLElement,
    redraw: () => void,
    rowLabel: string,
    typed: string,
  ): HTMLInputElement {
    const menu = openRowMenuByContextClick(root, redraw, rowLabel);
    const renameItem = Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).find(
      (element) => element.textContent === "Rename",
    );
    click(renameItem ?? null);
    redraw();

    const input = Array.from(root.querySelectorAll("input")).find(
      (element) => (element as HTMLInputElement).value === rowLabel,
    ) as HTMLInputElement | undefined;
    if (input === undefined) throw new Error("the row never became a rename field");
    input.value = typed;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    redraw();
    return input;
  }

  it("opens an inline rename field from the menu's Rename item, and commits it on blur", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const input = typeIntoRenameField(root, redraw, "Chat 1", "Renamed Chat");
    // The redraw inside the helper must not have put the row's stored label
    // back: mithril rewrites an input's `value` on every redraw unless the DOM
    // already holds exactly what the vnode declares.
    expect(input.value).toBe("Renamed Chat");

    input.dispatchEvent(new Event("blur"));
    redraw();
    expect(attrs.onRenameRow).toHaveBeenCalledWith(rows[0], "Renamed Chat");
  });

  it("discards the edit on Escape rather than committing it", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const input = typeIntoRenameField(root, redraw, "Chat 1", "Should not stick");
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    redraw();
    // The blur the field's own removal triggers must not resurrect the edit.
    input.dispatchEvent(new Event("blur"));
    redraw();

    expect(attrs.onRenameRow).not.toHaveBeenCalled();
    // The row reads as plain text again, not a field.
    expect(root.querySelector('input[value="Should not stick"]')).toBeNull();
  });

  it("folds the rail back up when a rename ends after the pointer has already gone", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    const slot = root.firstElementChild;
    slot?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const input = typeIntoRenameField(root, redraw, "Chat 1", "Renamed Chat");
    // The pointer leaves while the field is up. That leave is deliberately
    // swallowed -- collapsing mid-edit would commit a half-typed name -- and
    // it is the only one the browser sends, so nothing is left to fold the
    // rail up once the edit is over.
    slot?.dispatchEvent(new MouseEvent("mouseleave"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    input.dispatchEvent(new Event("blur"));
    redraw();
    expect(attrs.onRenameRow).toHaveBeenCalledWith(rows[0], "Renamed Chat");
    expect(root.querySelector(".project-rail-search")).toBeNull();
  });

  it("leaves the rail open when a rename ends with the pointer still on it", () => {
    const rows: SidebarTabRow[] = [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }];
    const attrs = makeAttrs({ rows });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const input = typeIntoRenameField(root, redraw, "Chat 1", "Renamed Chat");
    input.dispatchEvent(new Event("blur"));
    redraw();
    // No mouseleave yet: the pointer is still resting on the rail, and a real
    // one is still coming to collapse it whenever it goes.
    expect(root.querySelector(".project-rail-search")).not.toBeNull();
  });

  it("does not discard a rename in progress when the window blurs", () => {
    // Collapsing removes the input, and its removal commits what was typed --
    // so a blur mid-edit must leave the rail alone.
    const attrs = makeAttrs({
      rows: [{ ref: "chat:agent-9", kind: "chat", label: "Chat 9", isOpen: true }],
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    typeIntoRenameField(root, redraw, "Chat 9", "Renamed");

    window.dispatchEvent(new Event("blur"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();
  });

  it("ends a rename whose row stops being listed, rather than wedging the rail open", () => {
    // The object behind the row can be destroyed from another surface while
    // the field is up. Mithril then removes the focused input, which fires no
    // blur, so the field's own exit never runs -- and a `renamingRef` left set
    // makes every later mouseleave early-return.
    const attrs = makeAttrs({
      rows: [{ ref: "chat:agent-4", kind: "chat", label: "Chat 4", isOpen: true }],
    });
    const { root, redraw } = mountSidebar(attrs);
    const slot = root.firstElementChild;
    slot?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    typeIntoRenameField(root, redraw, "Chat 4", "Renamed");
    slot?.dispatchEvent(new MouseEvent("mouseleave"));
    redraw();
    expect(root.querySelector(".project-rail-search")).not.toBeNull();

    attrs.rows = [];
    redraw();
    // Nothing to name any more, so nothing is filed -- and the rail folds up
    // in place of the leave the edit swallowed on the way in.
    expect(attrs.onRenameRow).not.toHaveBeenCalled();
    expect(root.querySelector(".project-rail-search")).toBeNull();

    // Refs are handed out again (the terminal allocator reuses `terminal-N`),
    // so the next row answering to this one reads as plain text rather than a
    // field still holding the abandoned draft.
    attrs.rows = [{ ref: "terminal:terminal-4", kind: "terminal", label: "Terminal 4", isOpen: true }];
    slot?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();
    expect(Array.from(root.querySelectorAll("input")).some((element) => element.value === "Renamed")).toBe(false);
  });
});

describe("Sidebar tooltips", () => {
  // The custom hover bubble (see hoverTooltip.ts), not a native `title` --
  // that's the mechanism every workspace tooltip already uses, this rail's
  // shortcuts included, so the copy is what changed here, not how it shows.
  async function bubbleTextAfterHover(trigger: Element | null): Promise<string | null | undefined> {
    if (trigger === null) throw new Error("nothing to hover");
    trigger.dispatchEvent(new MouseEvent("mouseenter"));
    // Past the hover-intent delay (TOOLTIP_DELAY_MS = 250ms in hoverTooltip.ts).
    await new Promise((resolve) => setTimeout(resolve, 260));
    return document.querySelector(".minds-tooltip")?.textContent;
  }

  it("shows a static 'Switch projects' tooltip on the header, not the project's name", async () => {
    const { root } = mountSidebar(makeAttrs({ activeViewId: PROJECT_A.project_id }));
    expect(await bubbleTextAfterHover(root.querySelector(".project-rail-header"))).toBe("Switch projects");
  });

  it("carries the designed copy on the working shortcut rows, 'A agent chat' included", async () => {
    const { root } = mountSidebar(makeAttrs());
    const expectations: readonly [label: string, tooltip: string][] = [
      ["Chat", "A agent chat to work alongside you"],
      ["Browser", "A browser that agents can control on your behalf"],
      ["Terminal", "A terminal to run commands in your workspace"],
    ];
    for (const [label, tooltip] of expectations) {
      // hoverTooltipAttrs listens on the shortcut's wrapping span, not the
      // button nested inside it (see shortcutRow in Sidebar.ts).
      const button = Array.from(root.querySelectorAll(".project-rail-shortcut")).find((candidate) =>
        candidate.textContent?.includes(label),
      );
      expect(await bubbleTextAfterHover(button?.parentElement ?? null)).toBe(tooltip);
    }
  });
});

describe("Sidebar shortcut menus (modes)", () => {
  /** Opens one built-in shortcut row's own menu via its kebab. */
  function openShortcutMenu(root: HTMLElement, redraw: () => void, baseLabel: string): HTMLElement | null {
    const kebab = root.querySelector<HTMLElement>(`button[aria-label="Shortcut options for ${baseLabel}"]`);
    kebab?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    redraw();
    return root.querySelector<HTMLElement>('.project-rail-menu[role="menu"]');
  }

  function menuLabels(menu: HTMLElement | null): (string | null)[] {
    return Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).map((element) => element.textContent);
  }

  it("offers the focus-mode group on a focus-mode row: New X, the flip, and Unpin", () => {
    const attrs = makeAttrs();
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openShortcutMenu(root, redraw, "Terminal");
    expect(menuLabels(menu)).toEqual(["New Terminal", 'Change shortcut to "New Terminal"', "Unpin"]);
  });

  it("offers the new-mode group on a new-mode row, with Focus last disabled while the view shows none", () => {
    // Chat's default mode is new; the view lists no chat, so the
    // complementary focus is present but cannot act.
    const attrs = makeAttrs();
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openShortcutMenu(root, redraw, "Chat");
    expect(menuLabels(menu)).toEqual(["Focus last Chat", 'Change shortcut to "Chat"', "Unpin"]);
    const focusEntry = menu?.querySelector('[role="menuitem"][aria-disabled="true"]');
    expect(focusEntry?.textContent).toBe("Focus last Chat");
    focusEntry?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(attrs.onFocusLastOfKind).not.toHaveBeenCalled();
  });

  it("enables Focus last once the view shows one, and reports the focus", () => {
    const attrs = makeAttrs({ rows: [{ ref: "chat:agent-1", kind: "chat", label: "Chat 1", isOpen: true }] });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openShortcutMenu(root, redraw, "Chat");
    const focusEntry = Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).find(
      (element) => element.textContent === "Focus last Chat",
    );
    expect(focusEntry?.getAttribute("aria-disabled")).toBeNull();
    focusEntry?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(attrs.onFocusLastOfKind).toHaveBeenCalledWith("chat");
  });

  it("reports a mode flip through onSetShortcutMode", () => {
    const attrs = makeAttrs();
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openShortcutMenu(root, redraw, "Terminal");
    const flipEntry = Array.from(menu?.querySelectorAll('[role="menuitem"]') ?? []).find(
      (element) => element.textContent === 'Change shortcut to "New Terminal"',
    );
    flipEntry?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(attrs.onSetShortcutMode).toHaveBeenCalledWith("terminal", "new");
  });

  it("relabels a row from its stored mode override", () => {
    const flipped: ProjectInfo = { ...PROJECT_A, shortcut_overrides: { terminal: { mode: "new" } } };
    const { root, redraw } = mountSidebar(makeAttrs({ projects: [flipped, PROJECT_B], activeViewId: "project-a" }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const labels = Array.from(root.querySelectorAll(".project-rail-shortcut")).map((el) => el.textContent);
    expect(labels).toContain("New Terminal");
  });

  it("offers only the complementary action under Everything", () => {
    // Everything has no project entry: no mode to flip, nowhere to unpin.
    const attrs = makeAttrs({ activeViewId: EVERYTHING_VIEW_ID });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const menu = openShortcutMenu(root, redraw, "Terminal");
    expect(menuLabels(menu)).toEqual(["New Terminal"]);
  });

  it("offers History on the Browser and Terminal rows, keyed by their own services", () => {
    const ran: string[] = [];
    const attrs = makeAttrs({
      historyActionForService: vi.fn((serviceName: string) => () => ran.push(serviceName)),
    });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const browserMenu = openShortcutMenu(root, redraw, "Browser");
    expect(menuLabels(browserMenu)).toEqual(["History", "New Browser", 'Change shortcut to "New Browser"', "Unpin"]);
    Array.from(browserMenu?.querySelectorAll('[role="menuitem"]') ?? [])
      .find((element) => element.textContent === "History")
      ?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(ran).toEqual(["browser"]);
    expect(attrs.historyActionForService).toHaveBeenCalledWith("browser");

    redraw();
    expect(menuLabels(openShortcutMenu(root, redraw, "Terminal"))[0]).toBe("History");
    expect(attrs.historyActionForService).toHaveBeenCalledWith("terminal");
  });

  it("offers History on the File Viewer row, even unbacked", () => {
    const attrs = makeAttrs({ historyActionForService: vi.fn(() => () => undefined) });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuLabels(openShortcutMenu(root, redraw, "File Viewer"))).toEqual([
      "History",
      'Change shortcut to "New File Viewer"',
      "Unpin",
    ]);
    expect(attrs.historyActionForService).toHaveBeenCalledWith("files");
  });

  it("asks for no History on the Chat row, which is not versioned code", () => {
    const attrs = makeAttrs({ historyActionForService: vi.fn(() => () => undefined) });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuLabels(openShortcutMenu(root, redraw, "Chat"))).not.toContain("History");
    expect(attrs.historyActionForService).not.toHaveBeenCalledWith("chat");
  });

  it("leaves a shortcut menu unchanged where the app has no timeline", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ historyActionForService: vi.fn(() => null) }));
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    expect(menuLabels(openShortcutMenu(root, redraw, "Browser"))).toEqual([
      "New Browser",
      'Change shortcut to "New Browser"',
      "Unpin",
    ]);
  });

  it("stands a shortcut row down while its create is in flight", () => {
    const attrs = makeAttrs({ awaitingShortcutIds: new Set(["chat"]) });
    const { root, redraw } = mountSidebar(attrs);
    root.firstElementChild?.dispatchEvent(new MouseEvent("mouseenter"));
    redraw();

    const chatButton = Array.from(root.querySelectorAll("button.project-rail-shortcut")).find((el) =>
      el.textContent?.includes("Starting"),
    );
    expect(chatButton).not.toBeUndefined();
    expect(chatButton?.hasAttribute("disabled")).toBe(true);
    chatButton?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(attrs.onOpenTabType).not.toHaveBeenCalled();
  });
});
