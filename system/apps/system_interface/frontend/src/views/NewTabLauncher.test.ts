import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import type { MachineInventory, MemberKind } from "../models/Projects";
import {
  NewTabLauncher,
  buildLauncherRows,
  buildLauncherSections,
  filterRowsByKind,
  formatRecency,
  kindsInRows,
  sortRowsByRecency,
  type LauncherRow,
  type NewTabLauncherAttrs,
} from "./NewTabLauncher";

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const NOW = 1_700_000_000_000;

/** One row, with only the fields the logic under test reads. */
function row(ref: string, kind: MemberKind, label: string, lastActiveMs: number | null = null): LauncherRow {
  return { ref, kind, label, lastActiveMs };
}

const EMPTY_INVENTORY: MachineInventory = {
  chatAgents: [],
  terminals: [],
  browsers: [],
  apps: [],
  urlTabs: [],
};

describe("buildLauncherRows", () => {
  it("enumerates the machine in inventory order with the ref grammar", () => {
    const rows = buildLauncherRows(
      {
        ...EMPTY_INVENTORY,
        chatAgents: [{ name: "agent-1", label: "Build the Newsreader" }],
        terminals: [{ name: "build", label: "build" }],
        browsers: [{ name: "gazette", label: "Gazette (signed in)" }],
        apps: [{ name: "newsreader", label: "Newsreader" }],
      },
      {},
    );
    expect(rows.map((each) => [each.ref, each.kind])).toEqual([
      ["chat:agent-1", "chat"],
      ["terminal:build", "terminal"],
      ["service:browser?session=gazette", "browser"],
      ["service:newsreader", "app"],
    ]);
    expect(rows[0].label).toBe("Build the Newsreader");
  });

  it("decorates the rows it has a recency for and leaves the rest unknown", () => {
    const rows = buildLauncherRows(
      {
        ...EMPTY_INVENTORY,
        terminals: [
          { name: "build", label: "build" },
          { name: "logs", label: "logs" },
        ],
      },
      { "terminal:build": NOW - HOUR_MS },
    );
    expect(rows.map((each) => each.lastActiveMs)).toEqual([NOW - HOUR_MS, null]);
  });
});

describe("buildLauncherSections", () => {
  const ROWS = [
    row("service:newsreader", "app", "Newsreader"),
    row("chat:a1", "chat", "Fix source authorization"),
    row("terminal:build", "terminal", "build"),
  ];

  it("splits the machine into what this project shows and everything else", () => {
    const sections = buildLauncherSections(ROWS, ["service:newsreader", "terminal:build"], false);
    expect(sections.map((section) => section.key)).toEqual(["in-project", "on-machine"]);
    expect(sections[0].title).toBe("In this project");
    expect(sections[0].rows.map((each) => each.ref)).toEqual(["service:newsreader", "terminal:build"]);
    expect(sections[1].rows.map((each) => each.ref)).toEqual(["chat:a1"]);
  });

  it("files a row into the project only when it comes from the machine half", () => {
    const sections = buildLauncherSections(ROWS, ["service:newsreader"], false);
    expect(sections.map((section) => section.filesIntoProject)).toEqual([false, true]);
  });

  it("renders one machine-wide table for Everything, which has no member list", () => {
    const sections = buildLauncherSections(ROWS, [], true);
    expect(sections).toHaveLength(1);
    expect(sections[0].title).toBe("On this machine");
    expect(sections[0].rows.map((each) => each.ref)).toEqual(ROWS.map((each) => each.ref));
    // Everything is the unfiltered view: opening from it changes no membership.
    expect(sections[0].filesIntoProject).toBe(false);
  });

  it("puts every row in the machine half when the project shows nothing", () => {
    const sections = buildLauncherSections(ROWS, [], false);
    expect(sections[0].rows).toEqual([]);
    expect(sections[1].rows).toHaveLength(3);
  });
});

describe("filterRowsByKind", () => {
  const ROWS = [
    row("chat:a1", "chat", "Fix source authorization"),
    row("service:newsreader", "app", "Newsreader"),
    row("terminal:build", "terminal", "build"),
  ];

  it("keeps everything while nothing is unchecked", () => {
    expect(filterRowsByKind(ROWS, new Set())).toEqual(ROWS);
  });

  it("drops the kinds the user unchecked", () => {
    expect(filterRowsByKind(ROWS, new Set<MemberKind>(["chat", "terminal"])).map((each) => each.ref)).toEqual([
      "service:newsreader",
    ]);
  });

  it("shows a kind that only appears later, since the state names hidden kinds", () => {
    const hidden = new Set<MemberKind>(["chat"]);
    const withBrowser = [...ROWS, row("service:browser?session=gazette", "browser", "Gazette")];
    expect(filterRowsByKind(withBrowser, hidden).map((each) => each.kind)).toEqual(["app", "terminal", "browser"]);
  });
});

describe("sortRowsByRecency", () => {
  it("puts the most recently active first", () => {
    const rows = [
      row("chat:old", "chat", "old", NOW - 4 * DAY_MS),
      row("chat:new", "chat", "new", NOW - 60_000),
      row("chat:mid", "chat", "mid", NOW - 3 * HOUR_MS),
    ];
    expect(sortRowsByRecency(rows).map((each) => each.ref)).toEqual(["chat:new", "chat:mid", "chat:old"]);
  });

  it("sinks the rows with no known recency below the rest, in their own order", () => {
    const rows = [
      row("terminal:a", "terminal", "a"),
      row("chat:known", "chat", "known", NOW - DAY_MS),
      row("terminal:b", "terminal", "b"),
    ];
    expect(sortRowsByRecency(rows).map((each) => each.ref)).toEqual(["chat:known", "terminal:a", "terminal:b"]);
  });

  it("keeps the machine's order for rows of equal recency", () => {
    const rows = [row("terminal:first", "terminal", "first", NOW), row("terminal:second", "terminal", "second", NOW)];
    expect(sortRowsByRecency(rows).map((each) => each.ref)).toEqual(["terminal:first", "terminal:second"]);
  });

  it("leaves the caller's array alone", () => {
    const rows = [row("chat:old", "chat", "old", 1), row("chat:new", "chat", "new", 2)];
    sortRowsByRecency(rows);
    expect(rows.map((each) => each.ref)).toEqual(["chat:old", "chat:new"]);
  });
});

describe("kindsInRows", () => {
  it("offers each kind the table holds once, in the canonical order", () => {
    const rows = [
      row("service:newsreader", "app", "Newsreader"),
      row("chat:a1", "chat", "one"),
      row("chat:a2", "chat", "two"),
      row("terminal:build", "terminal", "build"),
    ];
    expect(kindsInRows(rows)).toEqual(["chat", "terminal", "app"]);
  });

  it("offers nothing for an empty table", () => {
    expect(kindsInRows([])).toEqual([]);
  });
});

describe("formatRecency", () => {
  it("reads coarsely, from just now out to weeks", () => {
    expect(formatRecency(NOW - 20_000, NOW)).toBe("just now");
    expect(formatRecency(NOW - 26 * 60_000, NOW)).toBe("26m ago");
    expect(formatRecency(NOW - 5 * HOUR_MS, NOW)).toBe("5h ago");
    expect(formatRecency(NOW - 4 * DAY_MS, NOW)).toBe("4d ago");
    expect(formatRecency(NOW - 9 * DAY_MS, NOW)).toBe("last week");
    expect(formatRecency(NOW - 30 * DAY_MS, NOW)).toBe("4w ago");
  });

  it("marks an unknown recency rather than guessing one", () => {
    expect(formatRecency(null, NOW)).toBe("—");
  });

  it("treats a clock ahead of ours as just now", () => {
    expect(formatRecency(NOW + 5_000, NOW)).toBe("just now");
  });
});

interface VnodeLike {
  tag?: unknown;
  attrs?: Record<string, unknown>;
  children?: unknown;
}

/** Depth-first walk of a rendered vnode tree, yielding every vnode and every
 *  text child. Lets the assertions below read the tree without a DOM. */
function flatten(node: unknown, out: unknown[] = []): unknown[] {
  if (node === null || node === undefined || typeof node === "boolean") return out;
  if (Array.isArray(node)) {
    for (const child of node) flatten(child, out);
    return out;
  }
  if (typeof node === "object" && "tag" in (node as VnodeLike)) {
    out.push(node);
    flatten((node as VnodeLike).children, out);
    return out;
  }
  out.push(node);
  return out;
}

function texts(node: unknown): string[] {
  return flatten(node).filter((each): each is string => typeof each === "string");
}

function tagsOf(tree: unknown, tag: string): VnodeLike[] {
  return flatten(tree).filter(
    (each): each is VnodeLike => typeof each === "object" && each !== null && (each as VnodeLike).tag === tag,
  );
}

/** Every button in render order: the four tiles, then per table its funnel
 *  followed by its rows. */
function buttonsOf(tree: unknown): VnodeLike[] {
  return tagsOf(tree, "button");
}

/** The open filter menu's checkboxes, one per kind that table holds. */
function inputsOf(tree: unknown): VnodeLike[] {
  return tagsOf(tree, "input");
}

const MACHINE_ROWS: LauncherRow[] = [
  row("chat:a1", "chat", "Fix source authorization", NOW - 26 * 60_000),
  row("service:newsreader", "app", "Newsreader", NOW - 4 * DAY_MS),
  row("service:gtd", "app", "GTD", NOW - 5 * HOUR_MS),
];

type LauncherView = ReturnType<typeof NewTabLauncher>["view"];

/** The launcher posed with a project showing the chat and the Newsreader, and
 *  GTD left over as the rest of the machine. */
function launcherAttrs(overrides: Partial<NewTabLauncherAttrs> = {}): NewTabLauncherAttrs {
  return {
    rows: MACHINE_ROWS,
    memberRefs: ["chat:a1", "service:newsreader"],
    isEverything: false,
    nowMs: NOW,
    onOpenNew: () => {},
    onOpenMember: () => {},
    onOpenFromMachine: () => {},
    ...overrides,
  };
}

function render(overrides: Partial<NewTabLauncherAttrs> = {}): unknown {
  const component = NewTabLauncher();
  return component.view({ attrs: launcherAttrs(overrides) } as Parameters<LauncherView>[0]);
}

describe("NewTabLauncher", () => {
  it("offers the four Open new tiles, with the file viewer inert until an app backs it", () => {
    const tiles = buttonsOf(render()).slice(0, 4);
    // Each tile renders its glyph markup and then its label.
    expect(tiles.map((tile) => texts(tile.children)[1])).toEqual(["Chat", "File viewer", "Browser", "Terminal"]);
    expect(tiles[1].attrs?.["aria-disabled"]).toBe("true");
    expect(tiles[1].attrs?.onclick).toBeUndefined();
    expect(tiles[0].attrs?.onclick).toBeTypeOf("function");
  });

  it("starts a new object of the tile's kind", () => {
    const started: string[] = [];
    const tiles = buttonsOf(render({ onOpenNew: (kind) => started.push(kind) }));
    (tiles[3].attrs?.onclick as () => void)();
    expect(started).toEqual(["terminal"]);
  });

  it("splits the machine into the two tables, most recent first", () => {
    const rendered = texts(render());
    expect(rendered).toContain("In this project");
    expect(rendered).toContain("On this machine");
    // The project shows the chat and the Newsreader; GTD is the rest of the
    // machine. Within the project table the chat is the more recent of the two.
    expect(rendered.indexOf("Fix source authorization")).toBeLessThan(rendered.indexOf("Newsreader"));
    expect(rendered).toContain("26m ago");
    expect(rendered).toContain("4d ago");
    expect(rendered).toContain("GTD");
  });

  it("renders the single machine-wide table for Everything", () => {
    const rendered = texts(render({ isEverything: true, memberRefs: [] }));
    expect(rendered).not.toContain("In this project");
    expect(rendered).toContain("On this machine");
    expect(rendered).toContain("Fix source authorization");
    expect(rendered).toContain("GTD");
  });

  it("opens a member without touching membership, and files a machine row in", () => {
    const opened: string[] = [];
    const filed: string[] = [];
    // After the four tiles and the "In this project" funnel come that table's
    // two rows, then the machine funnel and its single row.
    const buttons = buttonsOf(
      render({
        onOpenMember: (each) => opened.push(each.ref),
        onOpenFromMachine: (each) => filed.push(each.ref),
      }),
    );
    (buttons[5].attrs?.onclick as () => void)();
    (buttons[8].attrs?.onclick as () => void)();
    expect(opened).toEqual(["chat:a1"]);
    expect(filed).toEqual(["service:gtd"]);
  });

  it("hides the kinds unchecked in one table's filter, leaving the other alone", () => {
    const component = NewTabLauncher();
    const vnode = { attrs: launcherAttrs() } as Parameters<LauncherView>[0];

    // Open the "In this project" funnel, then uncheck its Chat box.
    (buttonsOf(component.view(vnode))[4].attrs?.onclick as () => void)();
    const checkbox = inputsOf(component.view(vnode))[0];
    expect(checkbox.attrs?.checked).toBe(true);
    (checkbox.attrs?.onchange as () => void)();

    const filtered = texts(component.view(vnode));
    expect(filtered).not.toContain("Fix source authorization");
    expect(filtered).toContain("Newsreader");
    // The machine table has its own filter, so its chat-free list is unchanged.
    expect(filtered).toContain("GTD");
  });

  it("says which table is empty, and distinguishes empty from filtered empty", () => {
    expect(texts(render({ memberRefs: [] }))).toContain("Nothing is in this project yet.");
    expect(texts(render({ memberRefs: MACHINE_ROWS.map((each) => each.ref) }))).toContain(
      "Nothing else is running on this machine.",
    );

    // Same table, but with rows the filter hid rather than none to begin with.
    const component = NewTabLauncher();
    const vnode = { attrs: launcherAttrs({ memberRefs: ["chat:a1"] }) } as Parameters<LauncherView>[0];
    (buttonsOf(component.view(vnode))[4].attrs?.onclick as () => void)();
    const checkbox = inputsOf(component.view(vnode))[0];
    (checkbox.attrs?.onchange as () => void)();
    expect(texts(component.view(vnode))).toContain("No tabs match this filter.");
  });
});
