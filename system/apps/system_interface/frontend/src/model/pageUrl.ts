/**
 * Where a window's page is: its app's origin plus the window's path.
 *
 * The origin is the app's, derived from its registered label on a workspace host exactly as
 * every app's is (see the library's origin.ts). A host with no workspace coordinate (a direct hit
 * on the loopback port, the e2e suite) has no origin family to derive into, so the app's
 * registered loopback URL is used instead. ``host`` and ``protocol`` are parameters so the
 * derivation is unit-testable without a DOM.
 */

import { LOCAL_HOSTNAMES, workspaceHostCoordinate } from "@imbue/workspace-ui/src/app_contract";
import { deriveAppOrigin } from "@imbue/workspace-ui/src/origin";
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

/** The domain the workspace was last shared under, read off its apps' share addresses, or null when it never was. */
export function shareDomainOf(apps: readonly Pick<AppRecord, "share_url">[]): string | null {
  for (const app of apps) {
    if (app.share_url === null) continue;
    try {
      return workspaceHostCoordinate(new URL(app.share_url).host.toLowerCase());
    } catch {
      continue;
    }
  }
  return null;
}

function loopbackUrl(url: string): URL | null {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  return LOCAL_HOSTNAMES.has(parsed.hostname) ? parsed : null;
}

/** The window a link to ``<app>.localhost`` (any port, usually none) is a page of: the app of ``apps`` whose label, or
 *  else name, is the host's one label, and the URL's path. This is how an agent links an app of a workspace never
 *  shared, with no port to go stale when the app's port changes. Null for any other URL. */
export function windowAtLocalAppHost(
  apps: readonly AppRecord[],
  url: string,
): { readonly app: AppRecord; readonly path: string } | null {
  let link: URL;
  try {
    link = new URL(url);
  } catch {
    return null;
  }
  const labels = link.hostname.toLowerCase().split(".");
  if (labels.length !== 2 || labels[1] !== "localhost") return null;
  const app =
    apps.find((candidate) => labelForApp(candidate) === labels[0]) ??
    apps.find((candidate) => candidate.name === labels[0]);
  return app === undefined ? null : { app, path: `${link.pathname}${link.search}` };
}

/** The window a local URL is a page of: the app of ``apps`` whose registered backend URL has the URL's scheme and
 *  port (on any loopback host name), and the URL's path. This is how a link an agent wrote to an app it runs by its
 *  backend URL (``http://localhost:<port>/...``) opens as that app's window. Null when none of ``apps`` is registered
 *  there. */
export function windowAtBackendUrl(
  apps: readonly AppRecord[],
  url: string,
): { readonly app: AppRecord; readonly path: string } | null {
  const link = loopbackUrl(url);
  if (link === null) return null;
  const app = apps.find((candidate) => {
    const backend = loopbackUrl(candidate.url);
    return backend !== null && backend.protocol === link.protocol && backend.port === link.port;
  });
  return app === undefined ? null : { app, path: `${link.pathname}${link.search}` };
}
