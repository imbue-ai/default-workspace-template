/**
 * Deep links (contracts.md section 13): ``/?view=<id>&open=<address>&action=<app>:<action-id>``,
 * honoured by the shell on load for the requesting client and then stripped from the URL. Unknown
 * or stale targets are ignored by whoever applies them; this module only reads and strips.
 */

export interface DeepLink {
  viewId: string | null;
  openAddress: string | null;
  action: { app: string; actionId: string } | null;
}

const VIEW_PARAM = "view";
const OPEN_PARAM = "open";
const ACTION_PARAM = "action";
const DEEP_LINK_PARAMS = [VIEW_PARAM, OPEN_PARAM, ACTION_PARAM, "follow"];

/** The deep link a query string carries; every field null when it carries none. */
export function parseDeepLink(search: string): DeepLink {
  const params = new URLSearchParams(search);
  const rawAction = params.get(ACTION_PARAM);
  const separator = rawAction?.indexOf(":") ?? -1;
  const action =
    rawAction !== null && separator > 0 && separator < rawAction.length - 1
      ? { app: rawAction.substring(0, separator), actionId: rawAction.substring(separator + 1) }
      : null;
  return {
    viewId: params.get(VIEW_PARAM) || null,
    openAddress: params.get(OPEN_PARAM) || null,
    action,
  };
}

/** Whether a deep link asks for anything. */
export function isDeepLinkEmpty(link: DeepLink): boolean {
  return link.viewId === null && link.openAddress === null && link.action === null;
}

/** The query string with the deep-link parameters removed (other parameters kept), "" when none remain. */
export function stripDeepLinkParams(search: string): string {
  const params = new URLSearchParams(search);
  for (const name of DEEP_LINK_PARAMS) params.delete(name);
  const remaining = params.toString();
  return remaining === "" ? "" : `?${remaining}`;
}
