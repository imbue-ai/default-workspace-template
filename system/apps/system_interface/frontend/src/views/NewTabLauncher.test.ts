import { describe, expect, it, vi } from "vitest";

// The launcher consults the alt-harness feature flag for its tile list. The
// real implementation caches a meta-tag read, which a test cannot safely poke;
// this keeps it a plain switch, off by default like the real flag.
let otherHarnessesEnabled = false;
let introductoryAgentsEnabled = false;
vi.mock("../base-path", () => ({
  areOtherHarnessesEnabled: () => otherHarnessesEnabled,
  areIntroductoryAgentsEnabled: () => introductoryAgentsEnabled,
}));

// The launcher asks the machine's app list whether a "files" app backs its
// file-viewer tile; a mutable stand-in lets each case pose a different machine
// (AgentManager's real list is module state fed by the WebSocket).
const appState: { apps: { name: string; url: string; label: string }[] } = { apps: [] };
vi.mock("../models/AgentManager", () => ({
  getApps: () => appState.apps,
}));

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
  openNewTiles,
  resetHiddenKinds,
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
  appInstances: [],
};

describe("buildLauncherRows", () => {
  it("enumerates the machine in inventory order with the ref grammar", () => {
    const rows = buildLauncherRows(
      {
        ...EMPTY_INVENTORY,
        chatAgents: [{ name: "agent-1", label: "Build the Newsreader" }],
        terminals: [{ name: "build", label: "build" }],
        browsers: [{ name: "gazette", label: "Gazette (signed in)" }],
        appInstances: [{ serviceName: "newsreader", instanceName: "newsreader-1", label: "Newsreader 1" }],
      },
      {},
    );
    expect(rows.map((each) => [each.ref, each.kind])).toEqual([
      ["chat:agent-1", "chat"],
      ["terminal:build", "terminal"],
      ["service:browser?session=gazette", "browser"],
      ["service:newsreader?instance=newsreader-1", "app"],
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
  const MACHINE = [
    row("service:newsreader", "app", "Newsreader"),
    row("chat:a1", "chat", "Fix source authorization"),
    row("terminal:build", "terminal", "build"),
  ];

  it("shows the member list under In this project, in member order", () => {
    const members = [row("terminal:build", "terminal", "build"), row("service:newsreader", "app", "Newsreader")];
    const sections = buildLauncherSections(MACHINE, members, false);
    expect(sections.map((section) => section.key)).toEqual(["in-project", "on-machine"]);
    expect(sections[0].title).toBe("In this project");
    expect(sections[0].rows.map((each) => each.ref)).toEqual(["terminal:build", "service:newsreader"]);
    expect(sections[1].rows.map((each) => each.ref)).toEqual(["chat:a1"]);
  });

  it("lists a backgrounded member the machine reports no live signal for", () => {
    // The rail lists this member, so the launcher must too: the in-project
    // table is the member list, not the machine's report of it.
    const backgrounded = row("url:deadbeef", "url", "Release notes");
    const sections = buildLauncherSections(MACHINE, [backgrounded], false);
    expect(sections[0].rows).toEqual([backgrounded]);
    expect(sections[1].rows.map((each) => each.ref)).toEqual(MACHINE.map((each) => each.ref));
  });

  it("never lists a member under On this machine", () => {
    const sections = buildLauncherSections(MACHINE, [row("chat:a1", "chat", "Fix source authorization")], false);
    expect(sections[1].rows.map((each) => each.ref)).toEqual(["service:newsreader", "terminal:build"]);
  });

  it("files a row into the project only when it comes from the machine half", () => {
    const sections = buildLauncherSections(MACHINE, [row("service:newsreader", "app", "Newsreader")], false);
    expect(sections.map((section) => section.filesIntoProject)).toEqual([false, true]);
  });

  it("renders one machine-wide table for Everything, which has no member list", () => {
    // Member rows are ignored outright: Everything shows the machine, whole.
    const sections = buildLauncherSections(MACHINE, [row("terminal:build", "terminal", "build")], true);
    expect(sections).toHaveLength(1);
    expect(sections[0].title).toBe("On this machine");
    expect(sections[0].rows.map((each) => each.ref)).toEqual(MACHINE.map((each) => each.ref));
    // Everything is the unfiltered view: opening from it changes no membership.
    expect(sections[0].filesIntoProject).toBe(false);
  });

  it("puts every row in the machine half when the project shows nothing", () => {
    const sections = buildLauncherSections(MACHINE, [], false);
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

describe("resetHiddenKinds", () => {
  it("re-shows everything by emptying the hidden set", () => {
    const hidden = new Set<MemberKind>(["chat", "terminal"]);
    resetHiddenKinds(hidden);
    expect(hidden.size).toBe(0);
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

/** Just the "Open new" tiles, which share a class no other button carries.
 *  `buttonsOf` also returns the tables' row buttons, which have no tile state
 *  of their own to assert on. */
function tilesOf(tree: unknown): VnodeLike[] {
  // Mithril moves a `class` attr onto `className` during normalization, so that
  // is where a rendered vnode's classes actually are.
  return buttonsOf(tree).filter((button) => String(button.attrs?.className ?? "").includes("new-tab-launcher-tile"));
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
    memberRows: [MACHINE_ROWS[0], MACHINE_ROWS[1]],
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

  it("lets the file viewer act once a files app backs it", () => {
    appState.apps = [{ name: "files", url: "http://files.test", label: "files-abc123" }];
    try {
      const tiles = buttonsOf(render()).slice(0, 4);
      expect(tiles[1].attrs?.["aria-disabled"]).toBeUndefined();
      expect(tiles[1].attrs?.onclick).toBeTypeOf("function");
    } finally {
      appState.apps = [];
    }
  });

  it("offers the harness tiles only where the host enables the alt harnesses", () => {
    // The real flag caches a meta-tag read; the mock above keeps it a plain
    // switch so this test cannot leak an enabled flag into its neighbours.
    otherHarnessesEnabled = true;
    try {
      const labels = buttonsOf(render()).map((tile) => texts(tile.children)[1]);
      expect(labels.slice(0, 3)).toEqual(["Chat", "Codex chat", "Pi chat"]);
    } finally {
      otherHarnessesEnabled = false;
    }
  });

  it("offers the introductory-chat tiles only where the host enables them", () => {
    introductoryAgentsEnabled = true;
    try {
      const labels = buttonsOf(render()).map((tile) => texts(tile.children)[1]);
      expect(labels.slice(0, 4)).toEqual([
        "Chat",
        "Introductory Claude chat",
        "Introductory Codex chat",
        "Introductory Pi chat",
      ]);
    } finally {
      introductoryAgentsEnabled = false;
    }
  });

  it("starts a new object of the tile's kind", () => {
    const started: string[] = [];
    const tiles = buttonsOf(render({ onOpenNew: (target) => started.push(target.kind) }));
    (tiles[3].attrs?.onclick as () => void)();
    expect(started).toEqual(["terminal"]);
  });

  it("hands the clicked tile's harness straight through", () => {
    otherHarnessesEnabled = true;
    try {
      const started: string[] = [];
      const tiles = buttonsOf(
        render({ onOpenNew: (target) => started.push(target.kind === "chat" ? target.harness : target.kind) }),
      );
      // Chat, Codex chat, Pi chat, then the non-chat tiles.
      (tiles[2].attrs?.onclick as () => void)();
      expect(started).toEqual(["pi-coding"]);
    } finally {
      otherHarnessesEnabled = false;
    }
  });

  it("stands every tile down while this pane is starting something", () => {
    // `mngr create` takes seconds. The launcher used to sit there untouched
    // for all of it, so an impatient second click started a SECOND object.
    const tiles = tilesOf(render({ isAwaitingCreate: true }));
    expect(tiles.length).toBeGreaterThan(0);
    for (const tile of tiles) {
      expect(tile.attrs?.["aria-disabled"]).toBe("true");
      expect(tile.attrs?.onclick).toBeUndefined();
    }
  });

  it("says it is starting, so the click is visibly acknowledged", () => {
    expect(texts(render({ isAwaitingCreate: true }))).toContain("Starting…");
    expect(texts(render())).not.toContain("Starting…");
  });

  it("drops the file-viewer tooltip while starting, since every tile is down", () => {
    // The tooltip explains why THAT one tile cannot act. Leaving it up while
    // all of them are down would explain the wrong thing. It is attached
    // through an `oncreate` hook (see hoverTooltipAttrs), so its absence is
    // what says the tooltip is gone.
    expect(tilesOf(render({ isAwaitingCreate: true })).some((tile) => tile.attrs?.oncreate !== undefined)).toBe(false);
    // ...and it is still there when the launcher is idle.
    expect(tilesOf(render()).some((tile) => tile.attrs?.oncreate !== undefined)).toBe(true);
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
    const rendered = texts(render({ isEverything: true }));
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

  it("arms Reset filters only once something is hidden, and it re-checks everything", () => {
    const component = NewTabLauncher();
    const vnode = { attrs: launcherAttrs() } as Parameters<LauncherView>[0];
    const resetOf = (rendered: unknown): VnodeLike =>
      buttonsOf(rendered).find((each) => texts(each.children).includes("Reset filters")) as VnodeLike;

    // Open the "In this project" funnel. Its menu names kinds in the plural,
    // and its reset row is inert while nothing is hidden.
    (buttonsOf(component.view(vnode))[4].attrs?.onclick as () => void)();
    let tree = component.view(vnode);
    expect(texts(tree)).toContain("Chats");
    expect(resetOf(tree).attrs?.disabled).toBe(true);

    // Hide chats: the reset row arms, and clicking it brings the chat back.
    (inputsOf(tree)[0].attrs?.onchange as () => void)();
    tree = component.view(vnode);
    expect(texts(tree)).not.toContain("Fix source authorization");
    expect(resetOf(tree).attrs?.disabled).toBe(false);
    (resetOf(tree).attrs?.onclick as () => void)();
    expect(texts(component.view(vnode))).toContain("Fix source authorization");
  });

  it("says which table is empty, and distinguishes empty from filtered empty", () => {
    expect(texts(render({ memberRows: [] }))).toContain("Nothing is in this project yet.");
    expect(texts(render({ memberRows: MACHINE_ROWS }))).toContain("Nothing else is running on this machine.");

    // Same table, but with rows the filter hid rather than none to begin with.
    const component = NewTabLauncher();
    const vnode = { attrs: launcherAttrs({ memberRows: [MACHINE_ROWS[0]] }) } as Parameters<LauncherView>[0];
    (buttonsOf(component.view(vnode))[4].attrs?.onclick as () => void)();
    const checkbox = inputsOf(component.view(vnode))[0];
    (checkbox.attrs?.onchange as () => void)();
    expect(texts(component.view(vnode))).toContain("No tabs match this filter.");
  });
});

describe("openNewTiles", () => {
  // A chat tile's harness reaches `mngr create --type` verbatim. The tile's own
  // word for pi is "Pi"; mngr's agent type is "pi-coding" ("pi" is only an
  // mngr-side alias, which the create endpoint's enum rejects outright), so a
  // tile carrying its own word instead of the agent type could not create at all.
  const harnessOf = (label: string) => {
    const target = openNewTiles().find((tile) => tile.label === label)?.target;
    return target?.kind === "chat" ? target.harness : undefined;
  };

  it("names mngr's agent type on every chat tile", () => {
    otherHarnessesEnabled = true;
    try {
      expect(harnessOf("Chat")).toBe("claude");
      expect(harnessOf("Codex chat")).toBe("codex");
      expect(harnessOf("Pi chat")).toBe("pi-coding");
      // Same trap as pi: the tile's word is "Agy", but mngr's agent type is
      // "antigravity" ("agy" is only an alias the create endpoint's enum rejects).
      expect(harnessOf("Agy chat")).toBe("antigravity");
    } finally {
      otherHarnessesEnabled = false;
    }
  });

  it("puts an introductory tile on its harness, asking only for the `first` template", () => {
    introductoryAgentsEnabled = true;
    try {
      expect(harnessOf("Introductory Pi chat")).toBe("pi-coding");
      const intro = openNewTiles().find((tile) => tile.label === "Introductory Pi chat")?.target;
      expect(intro).toEqual({ kind: "chat", harness: "pi-coding", first: true });
      // The plain tiles are the same create without that template.
      expect(openNewTiles().find((tile) => tile.label === "Chat")?.target).toEqual({
        kind: "chat",
        harness: "claude",
        first: false,
      });
    } finally {
      introductoryAgentsEnabled = false;
    }
  });

  it("gives the non-chat tiles no harness to send", () => {
    // Their kinds are the launcher's own vocabulary and never reach mngr.
    const kinds = openNewTiles()
      .filter((tile) => tile.target.kind !== "chat")
      .map((tile) => tile.target.kind);
    expect(kinds).toEqual(["files", "browser", "terminal"]);
  });
});
