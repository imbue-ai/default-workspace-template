import { describe, expect, it } from "vitest";

import {
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

const SERVED = new Set(["browser", "curio", "files", SYSTEM_HISTORY_APP_NAME, "terminal", VERSIONING_SERVICE_NAME]);

describe("appHistoryPath", () => {
  it("is the versioning app's per-app timeline route", () => {
    expect(appHistoryPath("curio")).toBe("/app/curio");
  });

  it("escapes a service name so it cannot break out of the path", () => {
    expect(appHistoryPath("a/b?c")).toBe("/app/a%2Fb%3Fc");
  });
});

describe("isHistoryService", () => {
  it("recognizes the versioning service and nothing else", () => {
    expect(isHistoryService(VERSIONING_SERVICE_NAME)).toBe(true);
    expect(isHistoryService("curio")).toBe(false);
  });

  it("answers no for a pane with no service behind it", () => {
    expect(isHistoryService(null)).toBe(false);
    expect(isHistoryService(undefined)).toBe(false);
  });
});

describe("isAppHistoryOffered", () => {
  it("offers History for an app the versioning app serves", () => {
    expect(isAppHistoryOffered([app("curio"), VERSIONING], SERVED, "curio")).toBe(true);
  });

  it("offers nothing when no versioning service is registered", () => {
    expect(isAppHistoryOffered([app("curio")], SERVED, "curio")).toBe(false);
  });

  it("still offers History when the versioning service is internal", () => {
    const apps = [app("curio"), app(VERSIONING_SERVICE_NAME, { internal: true })];
    expect(isAppHistoryOffered(apps, SERVED, "curio")).toBe(true);
  });

  it("refuses the workspace chrome under the name the registry spells it with", () => {
    expect(isAppHistoryOffered([app("system_interface"), VERSIONING], SERVED, "system_interface")).toBe(false);
  });

  it("offers the workspace chrome's own timeline under the name the versioning app serves", () => {
    expect(isAppHistoryOffered([VERSIONING], SERVED, SYSTEM_HISTORY_APP_NAME)).toBe(true);
  });

  it("refuses the versioning app itself", () => {
    expect(isAppHistoryOffered([VERSIONING], SERVED, VERSIONING_SERVICE_NAME)).toBe(false);
  });

  it("refuses any name the versioning app does not serve", () => {
    const apps = [app("si-preview"), app("owner-exec", { internal: true }), VERSIONING];
    expect(isAppHistoryOffered(apps, SERVED, "si-preview")).toBe(false);
    expect(isAppHistoryOffered(apps, SERVED, "owner-exec")).toBe(false);
    expect(isAppHistoryOffered(apps, SERVED, "removed-app")).toBe(false);
  });

  it("refuses everything until the served list has been fetched", () => {
    expect(isAppHistoryOffered([app("curio"), VERSIONING], null, "curio")).toBe(false);
  });

  it("does not care whether the versioning service is currently running", () => {
    const stopped = app(VERSIONING_SERVICE_NAME, { program: VERSIONING_SERVICE_NAME, is_running: false });
    expect(isAppHistoryOffered([app("curio"), stopped], SERVED, "curio")).toBe(true);
  });
});

describe("historyPaneMenuActions", () => {
  function labelsOf(actions: Parameters<typeof objectMenuEntries>[1]): string[] {
    return objectMenuEntries("app", actions).map((entry) => (entry === OBJECT_MENU_DIVIDER ? "---" : entry.label));
  }

  it("leaves a History pane's own tab exactly Refresh and Close tab", () => {
    const entries = labelsOf(
      historyPaneMenuActions({ refresh: () => {}, hideTab: () => {}, removeFromProject: null }),
    );
    expect(entries).toEqual(["Refresh", "---", "Close tab"]);
  });

  it("leaves a History row in the rail exactly Refresh and Remove from project", () => {
    const entries = labelsOf(
      historyPaneMenuActions({ refresh: () => {}, hideTab: null, removeFromProject: () => {} }),
    );
    expect(entries).toEqual(["Refresh", "---", "Remove from project"]);
  });

  it("leaves a History row under Everything just Refresh, with no trailing rule", () => {
    const entries = labelsOf(historyPaneMenuActions({ refresh: () => {}, hideTab: null, removeFromProject: null }));
    expect(entries).toEqual(["Refresh"]);
  });
});
