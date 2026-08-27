import { describe, expect, it, vi } from "vitest";

import { OBJECT_MENU_DIVIDER, objectMenuEntries, type ObjectMenuActions, type ObjectMenuKind } from "./objectMenu";

/** A full set of actions with every optional verb present, so each test only
 *  overrides what it cares about. */
function fullActions(overrides: Partial<ObjectMenuActions> = {}): ObjectMenuActions {
  return {
    refresh: vi.fn(),
    share: { label: "Share web", run: vi.fn() },
    history: vi.fn(),
    rename: vi.fn(),
    hideTab: vi.fn(),
    addToProjects: vi.fn(),
    removeFromProject: vi.fn(),
    stop: { label: "Stop Chat 1", run: vi.fn() },
    quit: { label: "Delete Chat 1", run: vi.fn() },
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

  it("withholds Rename from an object that supplies none, renameable kind or not", () => {
    // The per-OBJECT half of the same question: a kind can be renameable while
    // one particular object's name is still not the user's to choose, because
    // the shell derives it (the History pane, always called "History"). Such a
    // caller passes null, exactly as it does for every other verb it withholds
    // -- so the row is genuinely absent rather than present and inert.
    expect(labels(objectMenuEntries("chat", fullActions({ rename: null })))).not.toContain("Rename");
  });

  it("drops the divider when nothing would follow it", () => {
    // A backgrounded terminal still allocating its session has no rename, no
    // tab to hide and no destroy, so the menu must not end on a rule.
    const entries = objectMenuEntries("terminal", {
      ...fullActions(),
      hideTab: null,
      addToProjects: null,
      removeFromProject: null,
      stop: null,
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

  it("puts Remove from project between Close tab and the destroy", () => {
    // It is the middle of three easily-confused acts: drop the panel, drop the
    // filing, drop the object. Reading them in that order is what tells them
    // apart.
    const shown = labels(objectMenuEntries("chat", fullActions()));
    expect(shown.indexOf("Close tab")).toBeLessThan(shown.indexOf("Remove from project"));
    expect(shown.indexOf("Remove from project")).toBeLessThan(shown.indexOf("Delete Chat 1"));
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
    expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.iconName === "user-plus")).toBe(false);
  });

  it("offers History only to an app -- the one kind that is versioned code", () => {
    for (const kind of ["chat", "terminal", "browser"] as const) {
      expect(labels(objectMenuEntries(kind, fullActions()))).not.toContain("History");
    }
    expect(labels(objectMenuEntries("app", fullActions()))).toContain("History");
  });

  it("omits History for an app whose row would not earn its place", () => {
    // The versioning service is not registered, this app's timeline page would
    // 404 (the workspace chrome), or it is the versioning app itself -- which
    // the caller says by passing null, exactly as it does for an absent Share.
    const entries = objectMenuEntries("app", fullActions({ history: null }));
    expect(labels(entries)).not.toContain("History");
    expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.iconName === "history")).toBe(false);
  });

  it("puts History directly under Refresh, ahead of Share, with or without Share beside it", () => {
    // The two rows about what the tab is SHOWING read together at the top;
    // Share, which is about handing the service to someone else, follows. An
    // instance pane's Share moves to the trailing service group, and History
    // must not slide up or down when it does.
    const withShare = labels(objectMenuEntries("app", fullActions()));
    expect(withShare.slice(0, 3)).toEqual(["Refresh", "History", "Share web"]);
    const withoutShare = labels(objectMenuEntries("app", fullActions({ share: null })));
    expect(withoutShare.slice(0, 2)).toEqual(["Refresh", "History"]);
  });

  it("draws History behind the clock glyph and runs only the caller's callback", () => {
    const actions = fullActions();
    const entry = objectMenuEntries("app", actions).find(
      (candidate) => candidate !== OBJECT_MENU_DIVIDER && candidate.label === "History",
    );
    expect((entry as { iconName: string }).iconName).toBe("history");
    (entry as { run: () => void }).run();
    expect(actions.history).toHaveBeenCalledOnce();
    expect(actions.refresh).not.toHaveBeenCalled();
  });

  it("omits Close tab for a backgrounded object with no open tab", () => {
    const entries = objectMenuEntries("chat", fullActions({ hideTab: null }));
    expect(labels(entries)).not.toContain("Close tab");
  });

  it("includes Close tab when the object has an open tab", () => {
    const entries = objectMenuEntries("browser", fullActions());
    expect(labels(entries)).toContain("Close tab");
  });

  it("omits the destructive verb when quit is unavailable (e.g. the primary chat)", () => {
    const entries = objectMenuEntries("chat", fullActions({ quit: null }));
    expect(entries.some((entry) => entry !== OBJECT_MENU_DIVIDER && entry.isDestructive === true)).toBe(false);
  });

  it("labels the destructive verb with whatever name the caller supplies", () => {
    const entries = objectMenuEntries("terminal", fullActions({ quit: { label: "Delete Terminal 3", run: vi.fn() } }));
    const destructive = entries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.isDestructive === true);
    expect(destructive).not.toBeUndefined();
    expect((destructive as { label: string }).label).toBe("Delete Terminal 3");
  });

  it("runs the caller's callback and nobody else's", () => {
    const actions = fullActions();
    const entries = objectMenuEntries("app", actions);
    const refreshEntry = entries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Refresh");
    (refreshEntry as { run: () => void }).run();
    expect(actions.refresh).toHaveBeenCalledOnce();
    expect(actions.rename).not.toHaveBeenCalled();
  });

  it("keeps one divider between the acting group and the removal group", () => {
    const entries = objectMenuEntries("app", fullActions());
    expect(entries.filter((entry) => entry === OBJECT_MENU_DIVIDER)).toHaveLength(1);
    const dividerIndex = entries.indexOf(OBJECT_MENU_DIVIDER);
    // Refresh, History, Share and the filing verb come before it; removal follows.
    expect(labels(entries.slice(0, dividerIndex))).toEqual(["Refresh", "History", "Share web", "Add to project..."]);
    expect(entries[dividerIndex + 1]).not.toBe(OBJECT_MENU_DIVIDER);
  });

  it("puts the reversible stop ahead of the delete, never as the destructive row", () => {
    const entries = objectMenuEntries("chat", fullActions());
    const shown = labels(entries);
    expect(shown.indexOf("Stop Chat 1")).toBeLessThan(shown.indexOf("Delete Chat 1"));
    const stopEntry = entries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Stop Chat 1");
    expect((stopEntry as { isDestructive?: boolean }).isDestructive).toBeUndefined();
    expect((stopEntry as { iconName: string }).iconName).toBe("power");
  });

  it("gives the destructive delete the trash icon and a reversible quit slot the power icon", () => {
    // A chat's delete is destructive and reads behind the trash can; an app's
    // quit slot is the reversible Stop/Start, which keeps the power button.
    const deleteEntry = objectMenuEntries("chat", fullActions()).find(
      (entry) => entry !== OBJECT_MENU_DIVIDER && entry.isDestructive === true,
    );
    expect((deleteEntry as { iconName: string }).iconName).toBe("trash");
    const appEntries = objectMenuEntries(
      "app",
      fullActions({ stop: null, quit: { label: "Stop web", run: vi.fn(), isDestructive: false } }),
    );
    const stopSlot = appEntries.find((entry) => entry !== OBJECT_MENU_DIVIDER && entry.label === "Stop web");
    expect((stopSlot as { iconName: string }).iconName).toBe("power");
    expect((stopSlot as { isDestructive?: boolean }).isDestructive).toBe(false);
  });

  it("omits the filing verb and the stop when the caller has none to give", () => {
    const entries = objectMenuEntries("chat", fullActions({ addToProjects: null, stop: null }));
    const shown = labels(entries);
    expect(shown).not.toContain("Add to project...");
    expect(shown).not.toContain("Stop Chat 1");
  });

  it("produces the full fully-available list end to end", () => {
    expect(labels(objectMenuEntries("chat", fullActions()))).toEqual([
      "Refresh",
      "Add to project...",
      OBJECT_MENU_DIVIDER,
      "Rename",
      "Close tab",
      "Remove from project",
      "Stop Chat 1",
      "Delete Chat 1",
    ]);
    expect(labels(objectMenuEntries("app", fullActions({ stop: null })))).toEqual([
      "Refresh",
      "History",
      "Share web",
      "Add to project...",
      OBJECT_MENU_DIVIDER,
      "Close tab",
      "Remove from project",
      "Delete Chat 1",
    ]);
  });

  it("interleaves an instance menu's service verbs into the ordinary positions", () => {
    // An app-instance tab: the service's Share joins the acting group and its
    // Stop sits with the process verbs, directly above the instance's Delete.
    const entries = objectMenuEntries(
      "app",
      fullActions({
        share: null,
        stop: null,
        removeFromProject: null,
        quit: { label: "Delete Browser 1", run: vi.fn() },
        serviceGroup: {
          share: { label: "Share web", run: vi.fn() },
          lifecycle: { label: "Stop web", run: vi.fn() },
        },
      }),
    );
    expect(labels(entries)).toEqual([
      "Refresh",
      "History",
      "Share web",
      "Add to project...",
      OBJECT_MENU_DIVIDER,
      "Close tab",
      "Stop web",
      "Delete Browser 1",
    ]);
  });
});
