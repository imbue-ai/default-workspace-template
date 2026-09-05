import { describe, expect, it, vi } from "vitest";

import type { AppRecord, InstanceRecord } from "../models/Inventory";
import { TAB_MENU_DIVIDER, tabMenuEntries } from "./tabMenu";
import type { TabMenuActions } from "./tabMenu";

function app(overrides: Partial<AppRecord> = {}): AppRecord {
  return {
    name: "terminal",
    display_name: "Terminal",
    icon: "",
    label: "terminal-1a2b",
    url: "http://127.0.0.1:7681",
    internal: false,
    program: "terminal",
    critical: false,
    instances_url: "",
    has_instances: true,
    actions: [{ id: "new", label: "New terminal" }],
    default_shortcut: null,
    is_running: true,
    instances: [],
    ...overrides,
  };
}

function instance(overrides: Partial<InstanceRecord> = {}): InstanceRecord {
  return {
    key: "terminal-1",
    url: "/",
    title: "Terminal 1",
    status: "idle",
    lifetime: "referenced",
    last_active: null,
    renameable: true,
    ...overrides,
  };
}

function actions(overrides: Partial<TabMenuActions> = {}): TabMenuActions {
  return {
    refresh: vi.fn(),
    share: vi.fn(),
    addToProjects: vi.fn(),
    rename: vi.fn(),
    closeTab: vi.fn(),
    removeFromProject: null,
    setAppLifecycle: vi.fn(),
    delete: vi.fn(),
    ...overrides,
  };
}

function labels(entries: ReturnType<typeof tabMenuEntries>): string[] {
  return entries.map((entry) => (entry === TAB_MENU_DIVIDER ? "---" : entry.label));
}

describe("tabMenuEntries", () => {
  it("offers the acting group, then the removals in increasing severity", () => {
    expect(labels(tabMenuEntries(app(), instance(), actions()))).toEqual([
      "Refresh",
      "Share Terminal",
      "Add to project...",
      "---",
      "Rename",
      "Close tab",
      "Stop Terminal",
      "Delete Terminal 1",
    ]);
  });

  it("reads Start off a stopped app and withholds Stop from an app the workspace cannot stop", () => {
    expect(labels(tabMenuEntries(app({ is_running: false }), instance(), actions()))).toContain("Start Terminal");
    expect(labels(tabMenuEntries(app({ program: "" }), instance(), actions()))).not.toContain("Stop Terminal");
    expect(labels(tabMenuEntries(app({ critical: true }), instance(), actions()))).not.toContain("Stop Terminal");
  });

  it("withholds Rename from an instance its app does not rename, and Delete from a single-instance app", () => {
    const entries = labels(tabMenuEntries(app({ has_instances: false }), instance({ renameable: false }), actions()));
    expect(entries).not.toContain("Rename");
    expect(entries.some((label) => label.startsWith("Delete"))).toBe(false);
  });

  it("carries the rail's Remove from project and the tab's Close tab, whichever the caller supplies", () => {
    const rail = labels(tabMenuEntries(app(), instance(), actions({ closeTab: null, removeFromProject: vi.fn() })));
    expect(rail).toContain("Remove from project");
    expect(rail).not.toContain("Close tab");
  });

  it("omits Share when there is no share surface, and the divider when nothing follows it", () => {
    const entries = tabMenuEntries(
      app({ program: "", has_instances: false }),
      instance({ renameable: false }),
      actions({ share: null, closeTab: null }),
    );
    expect(labels(entries)).toEqual(["Refresh", "Add to project..."]);
  });

  it("runs the caller's callbacks", () => {
    const supplied = actions();
    const entries = tabMenuEntries(app(), instance(), supplied);
    for (const entry of entries) {
      if (entry !== TAB_MENU_DIVIDER) entry.run();
    }
    expect(supplied.refresh).toHaveBeenCalled();
    expect(supplied.delete).toHaveBeenCalled();
    expect(supplied.setAppLifecycle).toHaveBeenCalledWith("stop");
  });
});
