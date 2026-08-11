import { describe, expect, it, vi } from "vitest";

// Mithril captures `requestAnimationFrame` at import time so it can schedule
// redraws. Vitest's default (node) environment has no such global, so provide
// a polyfill before any import is evaluated.
vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

// The picker's only dependency on the live workspace is the app list, which
// AgentManager keeps in module state fed by the WebSocket. Swap it for a
// mutable stand-in so each case can pose a different machine.
const appState: { apps: { name: string; url: string; label: string }[] } = { apps: [] };

vi.mock("../models/AgentManager", () => ({
  getApps: () => appState.apps,
}));

import type { AppEntry } from "../models/AgentManager";
import { AllAppsPicker, filterApps, pickableApps } from "./AllAppsPicker";

// Deliberately unsorted, and deliberately including the chrome UI plus the two
// services that back a dedicated tab type.
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

function rowsOf(tree: unknown): VnodeLike[] {
  return flatten(tree).filter(
    (n): n is VnodeLike =>
      typeof n === "object" &&
      n !== null &&
      String((n as VnodeLike).attrs?.className ?? "").includes("layout-dialog-item"),
  );
}

function inputsOf(tree: unknown): VnodeLike[] {
  return flatten(tree).filter(
    (n): n is VnodeLike => typeof n === "object" && n !== null && (n as VnodeLike).tag === "input",
  );
}

type PickerView = ReturnType<typeof AllAppsPicker>["view"];

/** A stand-in vnode carrying just the attrs. The view reads nothing else off
 *  its vnode, so the lifecycle fields Mithril would supply are not needed. */
function makeVnode(onOpenApp: (app: AppEntry) => void): Parameters<PickerView>[0] {
  return { attrs: { onOpenApp, onCancel: () => {} } } as Parameters<PickerView>[0];
}

function render(onOpen: (appName: string) => void): unknown {
  const component = AllAppsPicker();
  return component.view(
    makeVnode((app) => {
      onOpen(app.name);
    }),
  );
}

describe("pickableApps", () => {
  it("keeps every app but the chrome UI, ordered by name", () => {
    appState.apps = APPS;
    expect(pickableApps().map((app) => app.name)).toEqual([
      "browser",
      "docs",
      "grafana",
      "kibana",
      "notebook",
      "pgadmin",
      "redis",
      "terminal",
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

describe("AllAppsPicker", () => {
  it("renders one row per app with an icon, the name, and a type label", () => {
    appState.apps = APPS;
    const rows = rowsOf(render(() => {}));
    expect(rows.length).toBe(9);
    expect(texts(rows[1].children)[0]).toContain("<svg");
    expect(texts(rows[1].children).slice(-2)).toEqual(["docs", "App"]);
    expect(rows[1].attrs?.title).toBe("A built app running in this machine");
  });

  it("names the two services that back a dedicated tab type", () => {
    appState.apps = APPS;
    const rows = rowsOf(render(() => {}));
    expect(texts(rows[0].children).slice(-2)).toEqual(["browser", "Browser"]);
    expect(texts(rows[7].children).slice(-2)).toEqual(["terminal", "Terminal"]);
  });

  it("hands the clicked app to the open callback", () => {
    appState.apps = APPS;
    const opened: string[] = [];
    const rows = rowsOf(render((appName) => opened.push(appName)));
    (rows[3].attrs?.onclick as () => void)();
    expect(opened).toEqual(["kibana"]);
  });

  it("shows the filter box only once the list is long", () => {
    appState.apps = APPS;
    expect(inputsOf(render(() => {})).length).toBe(1);
    appState.apps = APPS.slice(0, 3);
    expect(inputsOf(render(() => {})).length).toBe(0);
  });

  it("opens the top match when the filter box takes Enter", () => {
    appState.apps = APPS;
    const opened: string[] = [];
    const component = AllAppsPicker();
    const vnode = makeVnode((app) => {
      opened.push(app.name);
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
    expect(texts(render(() => {}))).toContain("No apps are running on this machine.");

    appState.apps = APPS;
    const component = AllAppsPicker();
    const vnode = makeVnode(() => {});
    const input = inputsOf(component.view(vnode))[0];
    (input.attrs?.oninput as (e: unknown) => void)({ target: { value: "zzz" } });
    expect(texts(component.view(vnode))).toContain('No apps match "zzz".');
  });
});
