// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { appRecord } from "../testing/records";
import { applyApps, resetInventoryForTesting } from "../models/Inventory";
import { AllAppsPicker, filterActions, pickableActions, pinKey, unpinnedActions } from "./AllAppsPicker";
import type { AllAppsPickerAttrs } from "./AllAppsPicker";

describe("pickableActions", () => {
  it("pairs every app with its primary action, ordered by display name, dropping apps with no actions", () => {
    const rows = pickableActions([
      appRecord("terminal"),
      appRecord("browser", { actions: [] }),
      appRecord("chat", {
        actions: [
          { id: "subagent", label: "Subagent" },
          { id: "new", label: "New Chat" },
        ],
        default_shortcut: { action: "new", mode: "new" },
      }),
    ]);
    expect(rows.map((row) => `${row.app.name}:${row.action.id}`)).toEqual(["chat:new", "terminal:new"]);
  });
});

describe("filterActions and unpinnedActions", () => {
  const rows = pickableActions([appRecord("terminal"), appRecord("files", { display_name: "File browser" })]);

  it("matches either name an app answers to, case-insensitively", () => {
    expect(filterActions(rows, "BROWSER").map((row) => row.app.name)).toEqual(["files"]);
    expect(filterActions(rows, "  ").length).toBe(2);
  });

  it("drops the rows a project already pinned", () => {
    expect(unpinnedActions(rows, [pinKey(rows[0])]).map((row) => row.app.name)).toEqual(["terminal"]);
  });
});

describe("AllAppsPicker", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
    resetInventoryForTesting();
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
    resetInventoryForTesting();
  });

  function mount(attrs: Partial<AllAppsPickerAttrs>): AllAppsPickerAttrs {
    const full: AllAppsPickerAttrs = {
      projectName: "Alpha",
      pinnedKeys: [],
      onRunAction: vi.fn(),
      onPin: vi.fn(),
      ...attrs,
    };
    m.mount(root, { view: () => m(AllAppsPicker, full) });
    return full;
  }

  it("lists unpinned apps with a pin under a project, runs the action on click, and pins on the toggle", () => {
    applyApps([appRecord("terminal"), appRecord("files"), appRecord("hidden", { internal: true })]);
    const attrs = mount({ pinnedKeys: ["files:new"] });
    const rows = Array.from(root.querySelectorAll<HTMLElement>("[data-app]"));
    expect(rows.map((row) => row.dataset.app)).toEqual(["terminal"]);
    rows[0].click();
    expect(attrs.onRunAction).toHaveBeenCalledWith(expect.objectContaining({ name: "terminal" }), {
      id: "new",
      label: "New terminal",
    });
    root.querySelector<HTMLElement>(".project-rail-pin")!.click();
    expect(attrs.onPin).toHaveBeenCalledWith(
      expect.objectContaining({ name: "terminal" }),
      expect.objectContaining({ id: "new" }),
    );
  });

  it("offers every app with no pins under Everything", () => {
    applyApps([appRecord("terminal"), appRecord("files")]);
    mount({ projectName: null, pinnedKeys: [] });
    expect(root.querySelectorAll("[data-app]").length).toBe(2);
    expect(root.querySelector(".project-rail-pin")).toBeNull();
  });

  it("says so when nothing is registered, and dims a stopped app", () => {
    mount({});
    expect(root.textContent).toContain("No apps are running on this machine.");
    applyApps([appRecord("terminal", { is_running: false })]);
    m.redraw.sync();
    expect(root.querySelector(".project-rail-app-stopped")).not.toBeNull();
  });
});
