import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The popover's only dependency on the live workspace is the app list, which
// AgentManager keeps in module state fed by the WebSocket. Swap it for a
// mutable stand-in so each case can pose a different machine.
const appState: { apps: { name: string; url: string; label: string }[] } = { apps: [] };

vi.mock("../models/AgentManager", () => ({
  getApps: () => appState.apps,
}));

import type { AppEntry } from "../models/AgentManager";
import { applyMemberTitleChange } from "../models/MemberTitles";
import { AllAppsPicker, filterApps, pickableApps, unpinnedApps } from "./AllAppsPicker";

// Deliberately unsorted, and deliberately including the chrome UI plus the two
// fleet services, all three of which the popover hides.
const APPS = [
  { name: "zulip", url: "http://zulip", label: "zulip-a1" },
  { name: "system_interface", url: "http://si", label: "system_interface-b2" },
  { name: "terminal", url: "http://term", label: "terminal-c3" },
  { name: "browser", url: "http://br", label: "browser-d4" },
  { name: "docs", url: "http://docs", label: "docs-e5" },
  { name: "grafana", url: "http://graf", label: "grafana-f6" },
  { name: "kibana", url: "http://kib", label: "kibana-g7" },
  { name: "notebook", url: "http://nb", label: "notebook-h8" },
  { name: "pgadmin", url: "http://pg", label: "pgadmin-i9" },
  { name: "redis", url: "http://redis", label: "redis-j0" },
];

// Two more, so the machine crosses the threshold at which the filter box
// appears. Neither name contains "an", so they stay out of the filter cases.
const EXTRA_APPS = [
  { name: "airflow", url: "http://af", label: "airflow-k1" },
  { name: "vault", url: "http://vault", label: "vault-l2" },
];

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
  return flatten(node).filter((n): n is string => typeof n === "string");
}

/** The app rows: the only vnodes in the tree that carry a key (the group
 *  headings and the filter box do not). */
function rowsOf(tree: unknown): VnodeLike[] {
  return flatten(tree).filter(
    (n): n is VnodeLike =>
      typeof n === "object" && n !== null && (n as VnodeLike).tag === "div" && "key" in ((n as VnodeLike).attrs ?? {}),
  );
}

function inputsOf(tree: unknown): VnodeLike[] {
  return flatten(tree).filter(
    (n): n is VnodeLike => typeof n === "object" && n !== null && (n as VnodeLike).tag === "input",
  );
}

/** The pin toggles: the only buttons this list draws. */
function buttonsOf(tree: unknown): VnodeLike[] {
  return flatten(tree).filter(
    (n): n is VnodeLike => typeof n === "object" && n !== null && (n as VnodeLike).tag === "button",
  );
}

type PickerView = ReturnType<typeof AllAppsPicker>["view"];

interface RenderOptions {
  // Undefined means the active view is a project called "Newsreader"; null
  // poses Everything, which pins nothing.
  projectName?: string | null;
  pinnedAppNames?: string[];
  onOpenApp?: (app: AppEntry) => void;
  onTogglePin?: (app: AppEntry, wanted: boolean) => void;
}

/** A stand-in vnode carrying just the attrs. The view reads nothing else off
 *  its vnode, so the lifecycle fields Mithril would supply are not needed. */
function makeVnode(options: RenderOptions): Parameters<PickerView>[0] {
  return {
    attrs: {
      projectName: options.projectName === undefined ? "Newsreader" : options.projectName,
      pinnedAppNames: options.pinnedAppNames ?? [],
      onOpenApp: options.onOpenApp ?? (() => {}),
      onTogglePin: options.onTogglePin ?? (() => {}),
    },
  } as unknown as Parameters<PickerView>[0];
}

function render(options: RenderOptions = {}): unknown {
  return AllAppsPicker().view(makeVnode(options));
}

describe("pickableApps", () => {
  it("hides the chrome UI and the fleet services, ordering the rest by name", () => {
    appState.apps = APPS;
    expect(pickableApps().map((app) => app.name)).toEqual([
      "docs",
      "grafana",
      "kibana",
      "notebook",
      "pgadmin",
      "redis",
      "zulip",
    ]);
  });

  it("hides an internal app -- a port with no page of its own", () => {
    // owner-exec: registered with forward_port.py --internal, so it has a URL
    // to forward but nothing to open. A different reason than the name-based
    // exclusions above, which all have a real page reached some other way.
    const withInternal: AppEntry[] = [
      ...APPS,
      { name: "owner-exec", url: "http://oe", label: "oe-1", internal: true },
    ];
    appState.apps = withInternal;
    expect(pickableApps().map((app) => app.name)).not.toContain("owner-exec");
  });
});

describe("filterApps", () => {
  it("matches a name substring case-insensitively", () => {
    expect(filterApps(APPS, "AN").map((app) => app.name)).toEqual(["grafana", "kibana"]);
  });

  it("treats a blank query as no filter", () => {
    expect(filterApps(APPS, "   ")).toEqual(APPS);
  });

  it("matches nothing when no name contains the query", () => {
    expect(filterApps(APPS, "zzz")).toEqual([]);
  });
});

describe("unpinnedApps", () => {
  it("excludes the project's pinned apps from the rest of the machine", () => {
    const rest = unpinnedApps(APPS, ["docs", "redis"]).map((app) => app.name);
    expect(rest).toContain("grafana");
    expect(rest).not.toContain("docs");
    expect(rest).not.toContain("redis");
  });

  it("ignores a pinned name the machine no longer offers", () => {
    // A member left behind by an app that has since been unregistered: it
    // addresses nothing in `apps`, so excluding it is a no-op rather than an
    // error.
    const rest = unpinnedApps(APPS, ["docs", "gone"]).map((app) => app.name);
    expect(rest).not.toContain("docs");
    expect(rest).toContain("grafana");
  });

  it("excludes nothing when the view pins nothing", () => {
    // Everything's case: the unfiltered view holds no members at all.
    expect(unpinnedApps(APPS, [])).toEqual([...APPS]);
  });
});

describe("AllAppsPicker", () => {
  it("excludes apps already pinned in the project, rather than heading them off", () => {
    // Pinned apps already have a row in the rail's own shortcuts -- see the
    // module docstring -- so this popover is for the other apps only. There is
    // no second group or heading left to hold them.
    appState.apps = APPS;
    const tree = render({ pinnedAppNames: ["docs", "redis"] });
    expect(rowsOf(tree).length).toBe(5);
    expect(texts(tree)).not.toContain("docs");
    expect(texts(tree)).not.toContain("redis");
    expect(texts(tree)).toContain("grafana");
    expect(texts(tree)).not.toContain("Pinned in Newsreader");
    expect(texts(tree)).not.toContain("Unpinned");
  });

  it("renders one flat list with no toggles under Everything", () => {
    // Everything pins nothing: every app on the machine is in its tab list
    // already, so there is no membership here to add or remove.
    appState.apps = APPS;
    const tree = render({ projectName: null });
    expect(rowsOf(tree).length).toBe(7);
    expect(texts(tree)).not.toContain("Unpinned");
    expect(buttonsOf(tree)).toEqual([]);
  });

  it("hands the clicked app to the open callback", () => {
    appState.apps = APPS;
    const opened: string[] = [];
    const rows = rowsOf(
      render({
        onOpenApp: (app) => {
          opened.push(app.name);
        },
      }),
    );
    (rows[2].attrs?.onclick as () => void)();
    expect(opened).toEqual(["kibana"]);
  });

  it("reports a pin without opening the app", () => {
    vi.useFakeTimers();
    try {
      appState.apps = APPS;
      const opened: string[] = [];
      const pinned: [string, boolean][] = [];
      const tree = render({
        onOpenApp: (app) => {
          opened.push(app.name);
        },
        onTogglePin: (app, wanted) => {
          pinned.push([app.name, wanted]);
        },
      });
      const pinButton = buttonsOf(rowsOf(tree)[0].children)[0];
      expect(pinButton.attrs?.["aria-label"]).toBe("Pin docs");
      let stopped = false;
      (pinButton.attrs?.onclick as (e: unknown) => void)({
        stopPropagation: () => {
          stopped = true;
        },
      });
      expect(pinned).toEqual([["docs", true]]);
      expect(stopped).toBe(true);
      expect(opened).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a just-pinned row on screen, fading, until its own transition finishes", () => {
    // The popover deliberately stays open so several apps can be pinned in
    // one visit -- an instant removal would risk a stray click landing on
    // whatever row slides up to fill the gap the instant the project's
    // member list catches up with the pin.
    vi.useFakeTimers();
    try {
      appState.apps = APPS;
      let pinnedAppNames: string[] = [];
      const component = AllAppsPicker();
      const vnode = () =>
        makeVnode({
          pinnedAppNames,
          onTogglePin: (app, wanted) => {
            if (wanted) pinnedAppNames = [...pinnedAppNames, app.name];
          },
        });

      const before = rowsOf(component.view(vnode()));
      expect(before.length).toBe(7);
      const pinButton = buttonsOf(before[0].children)[0];
      (pinButton.attrs?.onclick as (e: unknown) => void)({ stopPropagation: () => {} });

      // The click already asked the workspace to pin "docs" (and this stand-in
      // reflects that back into `pinnedAppNames` synchronously, the way a real
      // redraw eventually would) -- but the row itself is still rendered,
      // collapsing and fading rather than gone outright.
      const midFade = rowsOf(component.view(vnode()));
      expect(midFade.length).toBe(7);
      const fadingRow = midFade.find((row) => texts(row.children).includes("docs"));
      expect(fadingRow?.attrs?.className).toContain("opacity-0");
      expect(fadingRow?.attrs?.className).toContain("h-0");
      // No pin toggle on a row already on its way out.
      expect(buttonsOf(fadingRow?.children).length).toBe(0);

      vi.advanceTimersByTime(150);
      const after = rowsOf(component.view(vnode()));
      expect(after.length).toBe(6);
      expect(texts(after)).not.toContain("docs");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the filter box only once the list is long", () => {
    appState.apps = APPS;
    expect(inputsOf(render()).length).toBe(0);
    appState.apps = [...APPS, ...EXTRA_APPS];
    expect(inputsOf(render()).length).toBe(1);
  });

  it("opens the top match when the filter box takes Enter", () => {
    appState.apps = [...APPS, ...EXTRA_APPS];
    const opened: string[] = [];
    const component = AllAppsPicker();
    const vnode = makeVnode({
      onOpenApp: (app) => {
        opened.push(app.name);
      },
    });

    const input = inputsOf(component.view(vnode))[0];
    (input.attrs?.oninput as (e: unknown) => void)({ target: { value: "an" } });
    const filtered = component.view(vnode);
    expect(rowsOf(filtered).length).toBe(2);

    (inputsOf(filtered)[0].attrs?.onkeydown as (e: unknown) => void)({ key: "Enter" });
    expect(opened).toEqual(["grafana"]);
  });

  it("skips a pinned app when Enter opens the top match -- its row is not on screen", () => {
    appState.apps = [...APPS, ...EXTRA_APPS];
    const opened: string[] = [];
    const component = AllAppsPicker();
    const vnode = makeVnode({
      pinnedAppNames: ["airflow"],
      onOpenApp: (app) => {
        opened.push(app.name);
      },
    });

    const input = inputsOf(component.view(vnode))[0];
    (input.attrs?.oninput as (e: unknown) => void)({ target: { value: "a" } });
    const filtered = component.view(vnode);
    // Matches "a": airflow, grafana, kibana, pgadmin, vault -- airflow is
    // pinned and excluded, so only four rows are actually on screen.
    expect(rowsOf(filtered).length).toBe(4);

    (inputsOf(filtered)[0].attrs?.onkeydown as (e: unknown) => void)({ key: "Enter" });
    expect(opened).toEqual(["grafana"]);
  });

  it("distinguishes an empty machine, an empty filter result, and a fully-pinned project", () => {
    appState.apps = [];
    expect(texts(render())).toContain("No apps are running on this machine.");

    appState.apps = [...APPS, ...EXTRA_APPS];
    const component = AllAppsPicker();
    const vnode = makeVnode({});
    const input = inputsOf(component.view(vnode))[0];
    (input.attrs?.oninput as (e: unknown) => void)({ target: { value: "zzz" } });
    expect(texts(component.view(vnode))).toContain('No apps match "zzz".');

    // Every app the machine offers is already pinned: a new empty state, only
    // reachable now that a pinned app's row is excluded rather than merely
    // marked.
    appState.apps = APPS;
    const allPinnedNames = pickableApps().map((app) => app.name);
    expect(texts(render({ pinnedAppNames: allPinnedNames }))).toContain(
      "Every app on this machine is already pinned here.",
    );
  });
});

describe("AllAppsPicker names an app the way the rest of the workspace does", () => {
  /** Name "docs" for the length of one case, then put it back: the title store
   *  is module state shared by every test in this file. */
  function withRenamedDocs(chosenName: string, body: () => void): void {
    applyMemberTitleChange("service:docs", chosenName);
    try {
      body();
    } finally {
      applyMemberTitleChange("service:docs", null);
    }
  }

  it("shows the name the user gave an app, not the one it registered under", () => {
    // Rename is a verb on every kind now, apps included, and a name is filed by
    // ref machine-wide -- so this popover has to read the same store the rail,
    // the tab and the launcher read, or it is the one surface still calling the
    // app "docs".
    appState.apps = APPS;
    withRenamedDocs("Handbook", () => {
      const tree = render();
      expect(texts(tree)).toContain("Handbook");
      expect(texts(tree)).not.toContain("docs");
      const docsRow = rowsOf(tree).find((row) => row.attrs?.key === "docs");
      // The row is still keyed (and pinned) by the service name underneath.
      expect(docsRow).not.toBeUndefined();
      expect(buttonsOf(docsRow?.children)[0].attrs?.["aria-label"]).toBe("Pin Handbook");
    });
  });

  it("finds a renamed app by either name it answers to", () => {
    appState.apps = APPS;
    withRenamedDocs("Handbook", () => {
      const machineApps = pickableApps();
      // The name on the row, which is the only one the user can see here...
      expect(filterApps(machineApps, "handbook").map((app) => app.name)).toEqual(["docs"]);
      // ... and the registration the rest of the machine still addresses it by.
      expect(filterApps(machineApps, "docs").map((app) => app.name)).toEqual(["docs"]);
    });
  });
});
