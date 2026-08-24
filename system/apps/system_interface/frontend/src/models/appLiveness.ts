/**
 * Pure helpers over the app list's liveness fields.
 *
 * The registry row is an app's identity; whether it is running is derived
 * state the backend probes (supervisord for `program` rows, a TCP connect
 * otherwise) and pushes on `apps_updated`. These helpers are the one place
 * that derivation is read, so every surface that dims a stopped app -- the
 * rail, the tab list, the All apps popover, the launcher tables, the stopped
 * tab placeholder -- words and decides it identically.
 *
 * Kept apart from models/AgentManager (whose `getApps` is module state the
 * view tests mock wholesale) so the views can import these without every mock
 * having to restate them: callers pass the app (or the app list) in.
 */

import type { AppEntry } from "./AgentManager";

/** Whether an app is up. An absent flag reads as running -- a server predating
 *  liveness never says, and everything rendered before the field existed. */
export function isAppRunning(app: AppEntry): boolean {
  return app.is_running !== false;
}

/** How a stopped app's state reads on a tooltip: a supervised app is simply
 *  "stopped" (the workspace can start it again), while a program-less row is
 *  down in a way nothing here controls. */
export function appStoppedDetail(app: AppEntry): string {
  return app.program ? "stopped" : "not running (managed outside the workspace)";
}

/** Whether the app can be stopped and started through the workspace: it is
 *  supervised (registered a `program`) and is not one of the essential
 *  services the backend refuses to stop. */
export function isAppStoppable(app: AppEntry): boolean {
  if (!app.program) return false;
  return app.name !== "system_interface" && app.name !== "terminal";
}

/** The registered app a service name resolves to when that app is stopped,
 *  else null -- one lookup for every "should this row dim" call site. */
export function stoppedAppForServiceName(apps: readonly AppEntry[], serviceName: string | null): AppEntry | null {
  if (serviceName === null) return null;
  const app = apps.find((candidate) => candidate.name === serviceName);
  if (app === undefined || isAppRunning(app)) return null;
  return app;
}
