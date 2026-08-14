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
import { AllAppsPicker, filterApps, partitionAppsByPin, pickableApps } from "./AllAppsPicker";

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

describe("partitionAppsByPin", () => {
  it("splits the project's pinned apps from the rest of the machine", () => {
    const split = partitionAppsByPin(APPS, ["docs", "redis"]);
    expect(split.pinned.map((app) => app.name)).toEqual(["docs", "redis"]);
    expect(split.unpinned.map((app) => app.name)).toContain("grafana");
    expect(split.unpinned.map((app) => app.name)).not.toContain("docs");
  });

  it("keeps the pinned group in member order rather than the machine's", () => {
    const split = partitionAppsByPin(APPS, ["redis", "docs"]);
    expect(split.pinned.map((app) => app.name)).toEqual(["redis", "docs"]);
  });

  it("drops a pinned name the machine no longer offers", () => {
    // A member left behind by an app that has since been unregistered.
    const split = partitionAppsByPin(APPS, ["docs", "gone"]);
    expect(split.pinned.map((app) => app.name)).toEqual(["docs"]);
  });

  it("leaves every app unpinned when the view pins nothing", () => {
    // Everything's case: the unfiltered view holds no members at all.
    const split = partitionAppsByPin(APPS, []);
    expect(split.pinned).toEqual([]);
    expect(split.unpinned).toEqual([...APPS]);
  });
});

describe("AllAppsPicker", () => {
  it("renders one row per app, under the two pin headings", () => {
    appState.apps = APPS;
    const tree = render({ pinnedAppNames: ["docs"] });
    expect(rowsOf(tree).length).toBe(7);
    expect(texts(tree)).toContain("Pinned in Newsreader");
    expect(texts(tree)).toContain("Unpinned");
  });

  it("clips a long project name in the pinned heading instead of wrapping it", () => {
    // The popover is a fixed 240px card, and a project name is user-chosen
    // free text -- nothing bounds its length. Clipped via truncate/title
    // rather than a hand-picked character count, so it still tracks actual
    // rendered width rather than an arbitrary string length.
    appState.apps = APPS;
    const longName = "A Very Long Project Name That Would Otherwise Wrap Onto Several Lines";
    const tree = render({ projectName: longName, pinnedAppNames: ["docs"] });
    const headingText = `Pinned in ${longName}`;
    expect(texts(tree)).toContain(headingText);
    const heading = flatten(tree).find(
      (n): n is VnodeLike => typeof n === "object" && n !== null && (n as VnodeLike).attrs?.title === headingText,
    );
    expect(heading?.attrs?.className).toContain("truncate");
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
    let stopped = false;
    (pinButton.attrs?.onclick as (e: unknown) => void)({
      stopPropagation: () => {
        stopped = true;
      },
    });
    expect(pinned).toEqual([["docs", true]]);
    expect(stopped).toBe(true);
    expect(opened).toEqual([]);
  });

  it("asks to unpin an app the project already pins", () => {
    appState.apps = APPS;
    const pinned: [string, boolean][] = [];
    const tree = render({
      pinnedAppNames: ["docs"],
      onTogglePin: (app, wanted) => {
        pinned.push([app.name, wanted]);
      },
    });
    const pinButton = buttonsOf(rowsOf(tree)[0].children)[0];
    expect(pinButton.attrs?.["aria-label"]).toBe("Unpin docs");
    (pinButton.attrs?.onclick as (e: unknown) => void)({ stopPropagation: () => {} });
    expect(pinned).toEqual([["docs", false]]);
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

  it("distinguishes an empty machine from an empty filter result", () => {
    appState.apps = [];
    expect(texts(render())).toContain("No apps are running on this machine.");

    appState.apps = [...APPS, ...EXTRA_APPS];
    const component = AllAppsPicker();
    const vnode = makeVnode({});
    const input = inputsOf(component.view(vnode))[0];
    (input.attrs?.oninput as (e: unknown) => void)({ target: { value: "zzz" } });
    expect(texts(component.view(vnode))).toContain('No apps match "zzz".');
  });
});
