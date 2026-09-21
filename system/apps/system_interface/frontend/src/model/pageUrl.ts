/**
 * Where a window's page is: its app's origin plus the window's path.
 *
 * The origin is the app's, derived from its registered label on a workspace host exactly as
 * every app's is (see the library's origin.ts). A host with no workspace coordinate (a direct hit
 * on the loopback port, the e2e suite) has no origin family to derive into, so the app's
 * registered loopback URL is used instead. ``host`` and ``protocol`` are parameters so the
 * derivation is unit-testable without a DOM.
 */

import { deriveAppOrigin, workspaceHostCoordinate } from "@imbue/workspace-ui/src/origin";
import type { AppRecord } from "./records";

/** The origin label an app's public origin uses: its registered label, else its name (a legacy row). */
export function labelForApp(app: Pick<AppRecord, "name" | "label">): string {
  return app.label !== "" ? app.label : app.name;
}

/** The app's origin without a trailing slash, for this shell's host. */
export function appOrigin(app: Pick<AppRecord, "name" | "label" | "url">, host: string, protocol: string): string {
  return workspaceHostCoordinate(host) === host
    ? app.url.replace(/\/$/, "")
    : deriveAppOrigin(labelForApp(app), host, protocol).replace(/\/$/, "");
}

/** The URL a window's page loads at: the app's origin plus the window's path. */
export function windowPageUrl(
  app: Pick<AppRecord, "name" | "label" | "url">,
  path: string,
  host: string,
  protocol: string,
): string {
  return `${appOrigin(app, host, protocol)}${path.startsWith("/") ? path : `/${path}`}`;
}
