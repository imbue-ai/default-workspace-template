import { describe, expect, it, vi } from "vitest";

import type { AppRecord } from "../models/Inventory";
import { TAB_MENU_DIVIDER, tabMenuEntries } from "./tabMenu";
import type { TabMenuActions } from "./tabMenu";
import { appRecord, instanceRecord } from "../testing/records";

function app(overrides: Partial<AppRecord> = {}): AppRecord {
  return appRecord("terminal", { label: "terminal-1a2b", url: "http://127.0.0.1:7681", ...overrides });
}

const instance = instanceRecord;

function actions(overrides: Partial<TabMenuActions> = {}): TabMenuActions {
  return {
    refresh: vi.fn(),
    share: vi.fn(),
    addToProjects: vi.fn(),
    rename: vi.fn(),
    closeTab: vi.fn(),
    removeFromProject: null,
    setInstanceLifecycle: vi.fn(),
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
    expect(labels(tabMenuEntries(app(), instance({ stoppable: true }), actions()))).toEqual([
      "Refresh",
      "Share Terminal",
      "Add to project...",
      "---",
      "Rename",
      "Close tab",
      "Stop Terminal 1",
      "Delete Terminal 1",
    ]);
  });

  it("stops and starts the instance where its app reports it stoppable, reading the verb off its status", () => {
    const supplied = actions();
    const stopped = instance({ stoppable: true, status: "stopped" });
    expect(labels(tabMenuEntries(app(), stopped, supplied))).toContain("Start Terminal 1");
    const entries = tabMenuEntries(app(), stopped, supplied);
    for (const entry of entries) {
      if (entry !== TAB_MENU_DIVIDER && entry.label === "Start Terminal 1") entry.run();
    }
    expect(supplied.setInstanceLifecycle).toHaveBeenCalledWith("start");
    expect(labels(tabMenuEntries(app(), instance({ stoppable: false }), supplied))).not.toContain("Stop Terminal 1");
  });

  it("offers neither Stop nor Start of an instance while its app is down", () => {
    // The inventory reads every instance of a stopped app as stopped; a start would only reach an
    // unreachable app, and the app itself is started from the rail.
    const entries = labels(
      tabMenuEntries(app({ is_running: false }), instance({ stoppable: true, status: "stopped" }), actions()),
    );
    expect(entries).not.toContain("Start Terminal 1");
    expect(entries).not.toContain("Stop Terminal 1");
  });

  it("offers the app's own Stop and Start only on a single-instance app's tab", () => {
    // A multi-instance app is stopped from the rail, never from one instance's tab.
    expect(labels(tabMenuEntries(app(), instance(), actions()))).not.toContain("Stop Terminal");
    const single = app({ has_instances: false });
    expect(labels(tabMenuEntries(single, instance({ renameable: false }), actions()))).toContain("Stop Terminal");
    expect(labels(tabMenuEntries(app({ ...single, is_running: false }), instance(), actions()))).toContain(
      "Start Terminal",
    );
    expect(labels(tabMenuEntries(app({ ...single, program: "" }), instance(), actions()))).not.toContain(
      "Stop Terminal",
    );
    expect(labels(tabMenuEntries(app({ ...single, critical: true }), instance(), actions()))).not.toContain(
      "Stop Terminal",
    );
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
    const entries = tabMenuEntries(app(), instance({ stoppable: true }), supplied);
    for (const entry of entries) {
      if (entry !== TAB_MENU_DIVIDER) entry.run();
    }
    expect(supplied.refresh).toHaveBeenCalled();
    expect(supplied.delete).toHaveBeenCalled();
    expect(supplied.setInstanceLifecycle).toHaveBeenCalledWith("stop");
    const single = actions();
    for (const entry of tabMenuEntries(app({ has_instances: false }), instance(), single)) {
      if (entry !== TAB_MENU_DIVIDER) entry.run();
    }
    expect(single.setAppLifecycle).toHaveBeenCalledWith("stop");
  });
});
