// @vitest-environment jsdom
import "../testing/dom";

import { DockviewComponent, type IContentRenderer } from "dockview-core";
import { describe, expect, it } from "vitest";

import {
  isOwnSaveId,
  mintSaveId,
  mintTabId,
  panelParamsInDocument,
  panelsWithUnlistedAddresses,
  parsePanelParams,
} from "./Layouts";
import type { PanelParams } from "./Layouts";

describe("tab ids", () => {
  it("mints ids in the fixed shape, never twice", () => {
    const first = mintTabId();
    expect(first).toMatch(/^tab-[0-9a-f]{16}$/);
    expect(mintTabId()).not.toBe(first);
  });
});

describe("save ids", () => {
  it("mints ids in the fixed shape and remembers the ones this window minted", () => {
    const saveId = mintSaveId();
    expect(saveId).toMatch(/^save-[0-9a-f]{16}$/);
    expect(isOwnSaveId(saveId)).toBe(true);
    expect(isOwnSaveId("save-0123456789abcdef")).toBe(false);
    expect(mintSaveId()).not.toBe(saveId);
  });

  it("forgets the oldest ids once enough later ones were minted", () => {
    const oldest = mintSaveId();
    for (let index = 0; index < 64; index += 1) mintSaveId();
    expect(isOwnSaveId(oldest)).toBe(false);
  });
});

describe("parsePanelParams", () => {
  it("reads an instance's params, defaulting a missing focus stamp to never", () => {
    expect(parsePanelParams({ kind: "instance", address: "app:files", tabId: "tab-0000000000000001" })).toEqual({
      kind: "instance",
      address: "app:files",
      tabId: "tab-0000000000000001",
      lastFocusedMs: 0,
    });
    expect(
      parsePanelParams({ kind: "instance", address: "app:files", tabId: "tab-0000000000000001", lastFocusedMs: 42 }),
    ).toEqual({ kind: "instance", address: "app:files", tabId: "tab-0000000000000001", lastFocusedMs: 42 });
  });

  it("reads a launcher and rejects anything that names no instance", () => {
    expect(parsePanelParams({ kind: "launcher" })).toEqual({ kind: "launcher" });
    expect(parsePanelParams({ kind: "instance", address: "app:files" })).toBeNull();
    expect(parsePanelParams({ kind: "other" })).toBeNull();
    expect(parsePanelParams({})).toBeNull();
    expect(parsePanelParams(undefined)).toBeNull();
    expect(parsePanelParams("app:files")).toBeNull();
  });
});

describe("panelParamsInDocument", () => {
  it("collects the readable params of every panel a document names", () => {
    const document = {
      grid: { root: { type: "branch", data: [] }, width: 1, height: 1, orientation: "HORIZONTAL" },
      panels: {
        p1: { id: "p1", params: { kind: "instance", address: "app:files", tabId: "tab-0000000000000001" } },
        p2: { id: "p2", params: { kind: "launcher" } },
        p3: { id: "p3" },
      },
    };
    expect(panelParamsInDocument(document as never)).toEqual({
      p1: { kind: "instance", address: "app:files", tabId: "tab-0000000000000001", lastFocusedMs: 0 },
      p2: { kind: "launcher" },
    });
  });
});

describe("panelsWithUnlistedAddresses", () => {
  it("names the panels whose address no app lists any more, never a launcher", () => {
    const params: Record<string, PanelParams> = {
      p1: { kind: "instance", address: "app:files", tabId: "tab-0000000000000001", lastFocusedMs: 0 },
      p2: {
        kind: "instance",
        address: "app:terminal?instance=terminal-9",
        tabId: "tab-0000000000000002",
        lastFocusedMs: 0,
      },
      p3: { kind: "launcher" },
    };
    expect(panelsWithUnlistedAddresses(params, (address) => address === "app:files")).toEqual(["p2"]);
  });
});

/**
 * The design of this module rests on dockview owning a panel's params: what ``addPanel`` is
 * given reaches the renderer's ``init``, survives ``toJSON`` into ``fromJSON`` (where it reaches
 * ``init`` again), and ``updateParameters`` lands in the next ``toJSON``. Pinned here against the
 * installed dockview-core, since nothing else in this suite drives a real DockviewComponent.
 */
describe("dockview panel params", () => {
  function buildDock(seen: Record<string, unknown>[]): DockviewComponent {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const dock = new DockviewComponent(container, {
      createComponent(): IContentRenderer {
        return {
          element: document.createElement("div"),
          init(parameters) {
            seen.push(parameters.params);
          },
        };
      },
    });
    dock.layout(800, 600);
    return dock;
  }

  it("hands the params of addPanel to init, round-trips them through toJSON and fromJSON, and keeps updates", () => {
    const seen: Record<string, unknown>[] = [];
    const dock = buildDock(seen);
    const params: PanelParams = {
      kind: "instance",
      address: "app:files",
      tabId: "tab-0000000000000001",
      lastFocusedMs: 0,
    };
    dock.addPanel({ id: "tab-0000000000000001", component: "instance", title: "Files", params });
    expect(seen).toEqual([params]);

    dock.panels[0].api.updateParameters({ lastFocusedMs: 7 });
    const saved = dock.toJSON();
    expect(saved.panels["tab-0000000000000001"].params).toEqual({ ...params, lastFocusedMs: 7 });
    expect(parsePanelParams(dock.panels[0].params)).toEqual({ ...params, lastFocusedMs: 7 });

    const restoredSeen: Record<string, unknown>[] = [];
    const restored = buildDock(restoredSeen);
    restored.fromJSON(saved);
    expect(restoredSeen).toEqual([{ ...params, lastFocusedMs: 7 }]);
    expect(panelParamsInDocument(restored.toJSON())).toEqual({
      "tab-0000000000000001": { ...params, lastFocusedMs: 7 },
    });
  });
});
