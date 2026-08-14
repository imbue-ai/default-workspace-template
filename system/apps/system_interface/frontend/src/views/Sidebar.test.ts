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

// The rail reaches the live workspace only through the app list, which the
// helpers under test never consult.
vi.mock("../models/AgentManager", () => ({
  getApps: () => [],
}));

import m from "mithril";

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
    onOpenRow: vi.fn(),
    onRemoveFromView: vi.fn(),
    onShareApp: vi.fn(),
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
  it("opens on a header click and gives every project row its own edit pencil", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(root.querySelector('[aria-label="Project settings for Alpha"]')).not.toBeNull();
    expect(root.querySelector('[aria-label="Project settings for Beta"]')).not.toBeNull();
  });

  it("marks only the current project's row with the plain background -- no checkmark, no swapped icon", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ activeViewId: PROJECT_A.project_id }));
    click(root.querySelector(".project-rail-header"));
    redraw();
    expect(switcherRow(root, "Alpha").className).toContain("bg-bg-sidebar");
    expect(switcherRow(root, "Beta").className).not.toContain("bg-bg-sidebar");
  });

  it("marks Everything the same way when it is the active view, but gives it no edit pencil", () => {
    const { root, redraw } = mountSidebar(makeAttrs({ activeViewId: EVERYTHING_VIEW_ID }));
    click(root.querySelector(".project-rail-header"));
    redraw();
    const everythingRow = switcherRow(root, "Everything");
    expect(everythingRow.className).toContain("bg-bg-sidebar");
    expect(everythingRow.querySelector('[aria-label^="Project settings"]')).toBeNull();
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
    click(root.querySelector('[aria-label="Project settings for Beta"]'));
    redraw();
    expect(attrs.onSelectView).not.toHaveBeenCalled();
    const nameField = root.querySelector("input.custom-url-dialog-input") as HTMLInputElement | null;
    expect(nameField?.value).toBe("Beta");
  });

  it("sizes the dropdown to its own fixed width rather than the header's", () => {
    const { root, redraw } = mountSidebar(makeAttrs());
    click(root.querySelector(".project-rail-header"));
    redraw();
    const menu = root.querySelector('.project-rail-menu[role="menu"]') as HTMLElement | null;
    expect(menu?.style.width).toBe("280px");
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
