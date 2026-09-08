/**
 * Addresses: how every instance of every app is named across the workspace (contracts.md
 * section 1). ``app:<name>`` is an app (or its one instance); ``app:<name>?instance=<key>`` is
 * one instance. Pure string helpers, shared by the shell and every page that names an address.
 */

export const ADDRESS_SCHEME = "app:";
export const ADDRESS_INSTANCE_PARAMETER = "?instance=";

/** The address of ``app``'s instance ``key`` (``app:<name>?instance=<key>``), or of the app itself for "" (``app:<name>``). */
export function addressFor(appName: string, key: string): string {
  return key === "" ? `${ADDRESS_SCHEME}${appName}` : `${ADDRESS_SCHEME}${appName}${ADDRESS_INSTANCE_PARAMETER}${key}`;
}

/** The app and key an address names (``key`` is "" for the bare form), or null for anything else. */
export function parseAddress(address: string): { app: string; key: string } | null {
  if (!address.startsWith(ADDRESS_SCHEME)) return null;
  const body = address.substring(ADDRESS_SCHEME.length);
  const separator = body.indexOf("?");
  if (separator === -1) return body === "" ? null : { app: body, key: "" };
  const app = body.substring(0, separator);
  const remainder = body.substring(separator);
  if (app === "" || !remainder.startsWith(ADDRESS_INSTANCE_PARAMETER)) return null;
  const key = remainder.substring(ADDRESS_INSTANCE_PARAMETER.length);
  return key === "" ? null : { app, key };
}
