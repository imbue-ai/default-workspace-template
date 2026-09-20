/**
 * Deep links (desktop-interface contracts.md section 9): ``/?desktop=<id>&open=<app>:<path>&launch=<app>:<launch-id>``,
 * honoured by the shell on load for the requesting client and then stripped from the URL.
 * Unknown or stale targets are ignored by whoever applies them; this module only reads and strips.
 */

export interface DeepLinkOpen {
  readonly app: string;
  readonly path: string;
}

export interface DeepLinkLaunch {
  readonly app: string;
  readonly launch: string;
}

export interface DeepLink {
  readonly desktopId: string | null;
  readonly open: DeepLinkOpen | null;
  readonly launch: DeepLinkLaunch | null;
}

const DESKTOP_PARAM = "desktop";
const OPEN_PARAM = "open";
const LAUNCH_PARAM = "launch";
const DEEP_LINK_PARAMS = [DESKTOP_PARAM, OPEN_PARAM, LAUNCH_PARAM];

/** ``<app>:<rest>`` split at its first colon; null unless both halves are there. */
function splitTarget(raw: string | null): { app: string; rest: string } | null {
  if (raw === null) return null;
  const separator = raw.indexOf(":");
  if (separator <= 0 || separator >= raw.length - 1) return null;
  return { app: raw.substring(0, separator), rest: raw.substring(separator + 1) };
}

/** The deep link a query string carries; every field null when it carries none. */
export function parseDeepLink(search: string): DeepLink {
  const params = new URLSearchParams(search);
  const open = splitTarget(params.get(OPEN_PARAM));
  const launch = splitTarget(params.get(LAUNCH_PARAM));
  return {
    desktopId: params.get(DESKTOP_PARAM) || null,
    open: open !== null && open.rest.startsWith("/") ? { app: open.app, path: open.rest } : null,
    launch: launch === null ? null : { app: launch.app, launch: launch.rest },
  };
}

/** Whether a deep link asks for anything. */
export function isDeepLinkEmpty(link: DeepLink): boolean {
  return link.desktopId === null && link.open === null && link.launch === null;
}

/** The query string with the deep-link parameters removed (other parameters kept), "" when none remain. */
export function stripDeepLinkParams(search: string): string {
  const params = new URLSearchParams(search);
  for (const name of DEEP_LINK_PARAMS) params.delete(name);
  const remaining = params.toString();
  return remaining === "" ? "" : `?${remaining}`;
}
