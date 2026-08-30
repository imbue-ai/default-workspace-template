import type { AppEntry } from "../models/AgentManager";
import type { ObjectMenuActions } from "./objectMenu";

export const VERSIONING_SERVICE_NAME = "versioning";

export const SYSTEM_HISTORY_APP_NAME = "system-interface";

export const HISTORY_PANE_TITLE = "History";

export function appHistoryPath(serviceName: string): string {
  return `/app/${encodeURIComponent(serviceName)}`;
}

export function isHistoryService(serviceName: string | null | undefined): boolean {
  return serviceName === VERSIONING_SERVICE_NAME;
}

export function isAppHistoryOffered(
  apps: readonly AppEntry[],
  versionedAppNames: ReadonlySet<string> | null,
  serviceName: string,
): boolean {
  if (isHistoryService(serviceName)) return false;
  if (versionedAppNames === null || !versionedAppNames.has(serviceName)) return false;
  return apps.some((app) => app.name === VERSIONING_SERVICE_NAME);
}

export function historyPaneMenuActions(actions: {
  refresh: () => void;
  hideTab: (() => void) | null;
  removeFromProject: (() => void) | null;
}): ObjectMenuActions {
  return {
    refresh: actions.refresh,
    history: null,
    share: null,
    rename: null,
    hideTab: actions.hideTab,
    addToProjects: null,
    removeFromProject: actions.removeFromProject,
    stop: null,
    quit: null,
    serviceGroup: null,
  };
}
