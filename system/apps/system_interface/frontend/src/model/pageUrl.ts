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

/** The host names a backend URL's loopback host may be written as. */
const LOOPBACK_HOSTNAMES: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);

function loopbackUrl(url: string): URL | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  return LOOPBACK_HOSTNAMES.has(parsed.hostname) ? parsed : null;
}

/** The window a local URL is a page of: the openable app whose registered backend URL has the URL's scheme and
 *  port (on any loopback host name), and the URL's path. This is how a link an agent writes to an app it runs
 *  (``http://localhost:<port>/...``, the only address of it the agent knows) opens as that app's window. Null when
 *  no openable app is registered there. */
export function windowAtBackendUrl(
  apps: readonly AppRecord[],
  url: string,
): { readonly app: AppRecord; readonly path: string } | null {
  const link = loopbackUrl(url);
  if (link === null) return null;
  const app = apps.find((candidate) => {
    if (candidate.internal) return false;
    const backend = loopbackUrl(candidate.url);
    return backend !== null && backend.protocol === link.protocol && backend.port === link.port;
  });
  return app === undefined ? null : { app, path: `${link.pathname}${link.search}` };
}
