// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";

import { isPreviewShell } from "../models/PreviewShell";
import { stoppedPlaceholderForApp } from "./DockviewWorkspace";
import { TAB_MENU_DIVIDER, tabMenuEntries } from "./tabMenu";
import type { TabMenuActions } from "./tabMenu";
import { markPageAsPreviewShell } from "../testing/previewShell";
import { appRecord, instanceRecord } from "../testing/records";

function actions(): TabMenuActions {
  return {
    refresh: vi.fn(),
    share: null,
    addToProjects: vi.fn(),
    rename: vi.fn(),
    closeTab: vi.fn(),
    removeFromProject: null,
    setInstanceLifecycle: vi.fn(),
    setAppLifecycle: vi.fn(),
    delete: vi.fn(),
  };
}

function labels(app = appRecord("terminal"), instance = instanceRecord({ stoppable: true })): string[] {
  return tabMenuEntries(app, instance, actions()).map((entry) => (entry === TAB_MENU_DIVIDER ? "---" : entry.label));
}

describe("a preview shell", () => {
  let restore: (() => void) | null = null;

  afterEach(() => {
    restore?.();
    restore = null;
  });

  it("is what the page's meta tag says it is", () => {
    expect(isPreviewShell()).toBe(false);
    restore = markPageAsPreviewShell();
    expect(isPreviewShell()).toBe(true);
  });

  it("keeps the tab and filing verbs and drops every verb that would act on the live instance", () => {
    expect(labels()).toEqual([
      "Refresh",
      "Add to project...",
      "---",
      "Rename",
      "Close tab",
      "Stop Terminal 1",
      "Delete Terminal 1",
    ]);
    restore = markPageAsPreviewShell();
    expect(labels()).toEqual(["Refresh", "Add to project...", "---", "Close tab"]);
    expect(labels(appRecord("docs", { has_instances: false }), instanceRecord({ renameable: false }))).toEqual([
      "Refresh",
      "Add to project...",
      "---",
      "Close tab",
    ]);
  });

  it("offers no Start on a stopped app's placeholder", () => {
    expect(stoppedPlaceholderForApp(appRecord("docs", { is_running: false }))?.onStart).not.toBeNull();
    restore = markPageAsPreviewShell();
    expect(stoppedPlaceholderForApp(appRecord("docs", { is_running: false }))?.onStart).toBeNull();
  });
});
