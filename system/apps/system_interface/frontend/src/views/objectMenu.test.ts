import { describe, expect, it, vi } from "vitest";

import { OBJECT_MENU_DIVIDER, objectMenuEntries, type ObjectMenuActions, type ObjectMenuKind } from "./objectMenu";

/** A full set of actions with every optional verb present, so each test only
 *  overrides what it cares about. */
function fullActions(overrides: Partial<ObjectMenuActions> = {}): ObjectMenuActions {
  return {
    refresh: vi.fn(),
    share: { label: "Share web", run: vi.fn() },
    rename: vi.fn(),
    hideTab: vi.fn(),
    removeFromProject: vi.fn(),
    quit: { label: "Quit Chat 1", run: vi.fn() },
    ...overrides,
  };
}

function labels(entries: ReturnType<typeof objectMenuEntries>): (string | typeof OBJECT_MENU_DIVIDER)[] {
  return entries.map((entry) => (entry === OBJECT_MENU_DIVIDER ? entry : entry.label));
}

describe("objectMenuEntries", () => {
  it("offers Refresh to all four kinds", () => {
    const kinds: ObjectMenuKind[] = ["chat", "terminal", "browser", "app"];
    for (const kind of kinds) {
      const entries = objectMenuEntries(kind, fullActions());
      expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Refresh")).toBe(true);
    }
  });

  it("offers Rename only to a chat, whose name is the user's to choose", () => {
    // A chat's ref is its stable agent id and `mngr rename` moves the name
    // everywhere the agent is known, so the name the user gives it is the name
    // anything else -- an agent included -- can refer to it by. No other kind
    // manages that: a terminal and a browser ARE their names (a tmux session, a
    // Chromium profile), and an app's registered service name is the only
    // handle `layout.py` accepts, so a renamed app could be read but not
    // addressed. See isRenameableKind.
    const renameable = (kind: ObjectMenuKind): boolean =>
      objectMenuEntries(kind, fullActions()).some(
        (entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Rename",
      );
    expect(renameable("chat")).toBe(true);
    expect(renameable("app")).toBe(false);
    expect(renameable("terminal")).toBe(false);
    expect(renameable("browser")).toBe(false);
  });

  it("drops the divider when nothing would follow it", () => {
    // A backgrounded terminal still allocating its session has no rename, no
    // tab to hide and no destroy, so the menu must not end on a rule.
    const entries = objectMenuEntries("terminal", {
      ...fullActions(),
      hideTab: null,
      removeFromProject: null,
      quit: null,
    });
    expect(entries).not.toContain(OBJECT_MENU_DIVIDER);
    expect(entries.map((entry) => (entry === OBJECT_MENU_DIVIDER ? "--" : entry.label))).toEqual(["Refresh"]);
  });

  it("offers Refresh to a terminal -- reattach, not reload, but still offered", () => {
    // The terminal used to be excluded from Refresh entirely; it now gets the
    // same entry as everything else (see DockviewWorkspace's refreshPanelContent
    // for what "Refresh" actually does to a terminal's live session).
    const entries = objectMenuEntries("terminal", fullActions());
    expect(labels(entries)).toContain("Refresh");
  });

  it("offers Remove from project wherever there is a project to remove from", () => {
    // Everything is the home -- an object leaves it only by being destroyed --
    // which the caller says by passing null, exactly as it does for a
    // backgrounded row's absent Hide tab.
    const inProject = objectMenuEntries("chat", fullActions());
    expect(labels(inProject)).toContain("Remove from project");
    const inEverything = objectMenuEntries("chat", fullActions({ removeFromProject: null }));
    expect(labels(inEverything)).not.toContain("Remove from project");
  });

  it("puts Remove from project between Hide tab and the destroy", () => {
    // It is the middle of three easily-confused acts: drop the panel, drop the
    // filing, drop the object. Reading them in that order is what tells them
    // apart.
    const shown = labels(objectMenuEntries("chat", fullActions()));
    expect(shown.indexOf("Hide tab")).toBeLessThan(shown.indexOf("Remove from project"));
    expect(shown.indexOf("Remove from project")).toBeLessThan(shown.indexOf("Quit Chat 1"));
  });

  it("offers Share only to an app", () => {
    for (const kind of ["chat", "terminal", "browser"] as const) {
      const entries = objectMenuEntries(kind, fullActions({ share: null }));
      expect(labels(entries)).not.toContain("Share web");
    }
    const appEntries = objectMenuEntries("app", fullActions());
    expect(labels(appEntries)).toContain("Share web");
  });

  it("never offers Share when the kind is app but the caller has none to give", () => {
    const entries = objectMenuEntries("app", fullActions({ share: null }));
    expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.iconName === "share")).toBe(false);
  });

  it("omits Hide tab for a backgrounded object with no open tab", () => {
    const entries = objectMenuEntries("chat", fullActions({ hideTab: null }));
    expect(labels(entries)).not.toContain("Hide tab");
  });

  it("includes Hide tab when the object has an open tab", () => {
    const entries = objectMenuEntries("browser", fullActions());
    expect(labels(entries)).toContain("Hide tab");
  });

  it("omits the destructive verb when quit is unavailable (e.g. the primary chat)", () => {
    const entries = objectMenuEntries("chat", fullActions({ quit: null }));
    expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.isDestructive === true)).toBe(false);
  });

  it("labels the destructive verb with whatever name the caller supplies", () => {
    const entries = objectMenuEntries("terminal", fullActions({ quit: { label: "Quit Terminal 3", run: vi.fn() } }));
    const destructive = entries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.isDestructive === true);
    expect(destructive).not.toBeUndefined();
    expect((destructive as { label: string }).label).toBe("Quit Terminal 3");
  });

  it("runs the caller's callback and nobody else's", () => {
    const actions = fullActions();
    const entries = objectMenuEntries("app", actions);
    const refreshEntry = entries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Refresh");
    (refreshEntry as { run: () => void }).run();
    expect(actions.refresh).toHaveBeenCalledOnce();
    expect(actions.rename).not.toHaveBeenCalled();
  });

  it("keeps one divider between the reload/share group and the rename/hide/destroy group", () => {
    const entries = objectMenuEntries("app", fullActions());
    expect(entries.filter((entry) => entry === OBJECT_MENU_DIVIDER)).toHaveLength(1);
    const dividerIndex = entries.indexOf(OBJECT_MENU_DIVIDER);
    // Refresh and Share come before it; the hide/destroy group follows.
    expect(labels(entries.slice(0, dividerIndex))).toEqual(["Refresh", "Share web"]);
    expect(entries[dividerIndex + 1]).not.toBe(OBJECT_MENU_DIVIDER);
  });

  it("produces the full four-kind, fully-available list end to end", () => {
    expect(labels(objectMenuEntries("chat", fullActions()))).toEqual([
      "Refresh",
      OBJECT_MENU_DIVIDER,
      "Rename",
      "Hide tab",
      "Remove from project",
      "Quit Chat 1",
    ]);
    expect(labels(objectMenuEntries("app", fullActions()))).toEqual([
      "Refresh",
      "Share web",
      OBJECT_MENU_DIVIDER,
      "Hide tab",
      "Remove from project",
      "Quit Chat 1",
    ]);
  });
});
