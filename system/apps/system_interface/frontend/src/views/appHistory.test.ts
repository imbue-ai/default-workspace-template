import { describe, expect, it } from "vitest";

import {
  HISTORY_PANE_TITLE,
  SYSTEM_HISTORY_APP_NAME,
  VERSIONING_SERVICE_NAME,
  appHistoryPath,
  historyPaneMenuActions,
  isAppHistoryOffered,
  isHistoryService,
} from "./appHistory";
import { OBJECT_MENU_DIVIDER, objectMenuEntries } from "./objectMenu";
import type { AppEntry } from "../models/AgentManager";

function app(name: string, overrides: Partial<AppEntry> = {}): AppEntry {
  return { name, url: `http://localhost/${name}`, label: `${name}-abcd1234`, ...overrides };
}

const VERSIONING = app(VERSIONING_SERVICE_NAME, { program: VERSIONING_SERVICE_NAME });

// What the versioning app's own `GET /api/apps` reports: one folder per app
// under `system/apps`, the shell's included (under its hyphenated name).
const SERVED = new Set(["browser", "curio", "files", SYSTEM_HISTORY_APP_NAME, "terminal", VERSIONING_SERVICE_NAME]);

describe("appHistoryPath", () => {
  it("is the versioning app's per-app timeline route", () => {
    expect(appHistoryPath("curio")).toBe("/app/curio");
  });

  it("escapes a service name so it cannot break out of the path", () => {
    // Registered names are tame, but the path is built from live registry data
    // and a name is not validated here.
    expect(appHistoryPath("a/b?c")).toBe("/app/a%2Fb%3Fc");
  });

  it("spells the path the timeline page itself beacons", () => {
    // The timeline posts `{type: "minds-location", path: "/app/" + APP_NAME}`
    // (see the versioning package's timeline.html), so a pane pointed here and
    // a pane reopened from the stored beacon address the same page. If these
    // two ever disagree, re-opening a saved layout silently lands elsewhere.
    expect(appHistoryPath("files")).toBe("/app/files");
  });
});

describe("isHistoryService", () => {
  it("recognizes the versioning service and nothing else", () => {
    expect(isHistoryService(VERSIONING_SERVICE_NAME)).toBe(true);
    expect(isHistoryService("curio")).toBe(false);
  });

  it("answers no for a pane with no service behind it", () => {
    // Every caller holds an optional service name (a chat pane, a terminal
    // still allocating), so the absent cases have to answer rather than throw.
    expect(isHistoryService(null)).toBe(false);
    expect(isHistoryService(undefined)).toBe(false);
  });
});

describe("isAppHistoryOffered", () => {
  it("offers History for an app the versioning app serves", () => {
    expect(isAppHistoryOffered([app("curio"), VERSIONING], SERVED, "curio")).toBe(true);
  });

  it("offers nothing when no versioning service is registered", () => {
    // A workspace can legitimately run without it, and a row that opens
    // nothing is worse than no row.
    expect(isAppHistoryOffered([app("curio")], SERVED, "curio")).toBe(false);
  });

  it("still offers History when the versioning service is internal", () => {
    // `internal` hides an app from the listings a user browses ("All apps",
    // the rail's shortcut rows) -- which is the whole point here, since the
    // timeline is reached from an app's menu instead. Hidden from a list is
    // not the same as unroutable, so this must keep working.
    const apps = [app("curio"), app(VERSIONING_SERVICE_NAME, { internal: true })];
    expect(isAppHistoryOffered(apps, SERVED, "curio")).toBe(true);
  });

  it("refuses the workspace chrome under the name the registry spells it with", () => {
    // The versioning app names apps after their folder with underscores turned
    // into hyphens, so it serves `system-interface` and knows nothing called
    // `system_interface` -- which is what the registry (and so any pane) says.
    expect(isAppHistoryOffered([app("system_interface"), VERSIONING], SERVED, "system_interface")).toBe(false);
  });

  it("offers the workspace chrome's own timeline under the name the versioning app serves", () => {
    // The shell is versioned like everything else; the System menu's row asks
    // by this name, and nothing has to register it for the row to be honest.
    expect(isAppHistoryOffered([VERSIONING], SERVED, SYSTEM_HISTORY_APP_NAME)).toBe(true);
  });

  it("refuses the versioning app itself", () => {
    // Its own page exists -- it is in the served list -- but a timeline of the
    // timeline is noise on the one tab already showing one.
    expect(isAppHistoryOffered([VERSIONING], SERVED, VERSIONING_SERVICE_NAME)).toBe(false);
  });

  it("refuses a registered service the versioning app does not serve", () => {
    // A port registered with no package of its own under `system/apps` --
    // `si-preview` while a preview is up, mngr's `owner-exec` -- so
    // `/app/<name>` answers 404. Registered and non-internal is NOT the
    // question: `si-preview` is both, and still has no timeline.
    const apps = [app("si-preview"), app("owner-exec", { internal: true }), VERSIONING];
    expect(isAppHistoryOffered(apps, SERVED, "si-preview")).toBe(false);
    expect(isAppHistoryOffered(apps, SERVED, "owner-exec")).toBe(false);
  });

  it("refuses a name the versioning app has never heard of", () => {
    // A pane whose app was removed or renamed. Its own placeholder already
    // says the app is gone; its menu must not offer a history of it.
    expect(isAppHistoryOffered([VERSIONING], SERVED, "removed-app")).toBe(false);
  });

  it("refuses everything until the served list has been fetched", () => {
    // Null is "not known yet" (or the versioning app could not be reached).
    // Guessing from the registry is exactly what the served list replaces, so
    // the answer while it is missing is no row rather than a plausible one.
    expect(isAppHistoryOffered([app("curio"), VERSIONING], null, "curio")).toBe(false);
  });

  it("does not care whether the versioning service is currently running", () => {
    // Identity is the registry row; liveness is derived. Asking for a stopped
    // app's history starts it, exactly as opening its tab does.
    const stopped = app(VERSIONING_SERVICE_NAME, { program: VERSIONING_SERVICE_NAME, is_running: false });
    expect(isAppHistoryOffered([app("curio"), stopped], SERVED, "curio")).toBe(true);
  });
});

describe("historyPaneMenuActions", () => {
  function labelsOf(actions: Parameters<typeof objectMenuEntries>[1]): string[] {
    return objectMenuEntries("app", actions).map((entry) => (entry === OBJECT_MENU_DIVIDER ? "---" : entry.label));
  }

  it("leaves a History pane's own tab exactly Refresh and Close tab", () => {
    // The whole point: a history is not an object the user made, so none of an
    // app's verbs mean anything on it -- and one of them (a destroy) would be
    // actively wrong. Asserted as the WHOLE list rather than as absences, so a
    // verb added to the app menu later cannot quietly appear here too.
    const entries = labelsOf(
      historyPaneMenuActions({ refresh: () => {}, hideTab: () => {}, removeFromProject: null }),
    );
    // The put-the-tab-away verb keeps this template's label ("Close tab").
    expect(entries).toEqual(["Refresh", "---", "Close tab"]);
  });

  it("leaves a History row in the rail exactly Refresh and Remove from project", () => {
    // The rail's own way of saying "stop showing this here" stands in for the
    // tab's Hide tab; nothing else survives here either.
    const entries = labelsOf(
      historyPaneMenuActions({ refresh: () => {}, hideTab: null, removeFromProject: () => {} }),
    );
    expect(entries).toEqual(["Refresh", "---", "Remove from project"]);
  });

  it("leaves a History row under Everything just Refresh, with no trailing rule", () => {
    // Everything is the home: nothing leaves it, so the row has nothing but a
    // reload -- and a one-entry menu must not end on a divider.
    const entries = labelsOf(historyPaneMenuActions({ refresh: () => {}, hideTab: null, removeFromProject: null }));
    expect(entries).toEqual(["Refresh"]);
  });

  it("runs the caller's own callbacks", () => {
    const ran: string[] = [];
    const entries = objectMenuEntries(
      "app",
      historyPaneMenuActions({
        refresh: () => ran.push("refresh"),
        hideTab: () => ran.push("hide"),
        removeFromProject: null,
      }),
    );
    for (const entry of entries) {
      if (entry !== OBJECT_MENU_DIVIDER) entry.run();
    }
    expect(ran).toEqual(["refresh", "hide"]);
  });
});

describe("HISTORY_PANE_TITLE", () => {
  it("is what every pane of the versioning service is called", () => {
    // Named here rather than at each surface so the tab, the rail row and the
    // launcher cannot drift apart -- and so nothing has to be filed in the
    // machine-wide title store to make a freshly opened pane read right.
    expect(HISTORY_PANE_TITLE).toBe("History");
  });
});
