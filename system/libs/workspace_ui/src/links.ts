/**
 * What a link means inside the workspace (the workspace link routing plan), so every place that decides where a link
 * goes agrees.
 *
 * - A web link off this machine (and ``mailto:``, ``tel:``) is **external**, by the app contract's ``isExternalUrl``,
 *   which a shared fixture holds to the Imbue Studio desktop app's rule: an app registered for its message
 *   (``open:web``, ``open:mailto``, ``open:tel``) opens it, else the user's own browser or mail or phone app.
 * - A ``file:`` URL on this machine, or an absolute path as written in a chat message, is a **file** of the
 *   workspace, opened in the File Viewer (``open:file``).
 * - A URL on a local host (``localhost``, ``127.0.0.1``, ``[::1]``, ``*.localhost``) is a **local URL**: on a bare
 *   host name, the window of the app registered at its port, else the workspace's browser (``open:url``). That is
 *   unless it is the address of an app of a workspace: one of this workspace's apps (an **app address**, opened as
 *   that app's window) or another workspace's (which the shell refuses).
 * - An address on this workspace's share domain is an **app address** too, from any client, though its host is not
 *   local; another workspace's share address cannot be told from any other site, so it is external.
 * - Anything else (a relative path, a fragment, another scheme) is **unroutable**.
 */

import { LOCAL_HOSTNAMES, isExternalUrl } from "./app_contract";
import { hasWorkspaceCoordinate, workspaceHostCoordinate } from "./origin";

/** What a link is, as the workspace routes it. */
export type LinkTarget =
  | { readonly kind: "external"; readonly url: string }
  | { readonly kind: "local-url"; readonly url: string }
  | { readonly kind: "app-address"; readonly label: string; readonly path: string; readonly url: string }
  | { readonly kind: "other-workspace"; readonly url: string }
  | { readonly kind: "file"; readonly path: string }
  | { readonly kind: "unroutable" };

/** The message types a link becomes (desktop-interface contracts.md section 5.6): ``open:file`` for a file, and
 *  ``open:url`` for a local URL no app is registered at. */
export const OPEN_FILE_MESSAGE = "open:file";
export const OPEN_URL_MESSAGE = "open:url";

/** The message an external link becomes: ``open:mailto``, ``open:tel``, or ``open:web`` for a web link. Only an app
 *  registered for it takes it; with none, the link opens outside the workspace. */
export function externalLinkMessageType(url: string): "open:mailto" | "open:tel" | "open:web" {
  const scheme = url.slice(0, url.indexOf(":") + 1).toLowerCase();
  if (scheme === "mailto:") return "open:mailto";
  if (scheme === "tel:") return "open:tel";
  return "open:web";
}

const WEB_SCHEMES: ReadonlySet<string> = new Set(["http:", "https:"]);
const WEB_URL_PREFIX = /^https?:\/\//i;

/** The file a path names: decoded, with no query or fragment and no trailing slash (but the root's), so one file or
 *  folder has one spelling; null when a segment does not decode. */
function filePathOf(path: string): string | null {
  let decoded: string;
  try {
    decoded = path
      .split("/")
      .map((segment) => decodeURIComponent(segment))
      .join("/");
  } catch {
    return null;
  }
  return decoded.length > 1 ? decoded.replace(/\/+$/, "") : decoded;
}

/** The ``file:`` URL of an absolute path of this machine. */
export function fileUrl(path: string): string {
  return `file://${path
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/")}`;
}

/** The address ``url`` names on ``coordinate``, one of this workspace's: an app's, or unroutable for the bare
 *  coordinate. */
function appAddressOn(url: URL, host: string, coordinate: string): LinkTarget {
  const appLabels = host.slice(0, host.length - coordinate.length).replace(/\.$/, "");
  if (appLabels === "") return { kind: "unroutable" };
  return { kind: "app-address", label: appLabels.split(".")[0], path: `${url.pathname}${url.search}`, url: url.href };
}

/** A local URL, as this workspace sees it: one of its own apps' addresses, another workspace's, or neither. */
function classifyLocalUrl(url: URL, workspaceHost: string): LinkTarget {
  const host = url.host.toLowerCase();
  if (!hasWorkspaceCoordinate(host)) return { kind: "local-url", url: url.href };
  const coordinate = workspaceHostCoordinate(host);
  if (coordinate !== workspaceHostCoordinate(workspaceHost.toLowerCase())) {
    return { kind: "other-workspace", url: url.href };
  }
  return appAddressOn(url, host, coordinate);
}

/** The address a web URL names on a share domain of this workspace (the page's own, when it is served from one, or
 *  ``shareDomain``), or null for any other host. */
function shareAddressOf(url: URL, workspaceHost: string, shareDomain: string | null): LinkTarget | null {
  const host = url.host.toLowerCase();
  if (!hasWorkspaceCoordinate(host)) return null;
  const coordinate = workspaceHostCoordinate(host);
  const pageCoordinate = workspaceHostCoordinate(workspaceHost.toLowerCase());
  const isThisWorkspace =
    (shareDomain !== null && coordinate === shareDomain.toLowerCase()) ||
    (hasWorkspaceCoordinate(pageCoordinate) && coordinate === pageCoordinate);
  return isThisWorkspace ? appAddressOn(url, host, coordinate) : null;
}

/** What ``href`` (a URL, or a link's attribute as written in a chat message, where an absolute path names a file) is,
 *  for a page served under ``workspaceHost`` (a host of this workspace, such as the page's own ``location.host``).
 *  ``shareDomain`` is the domain this workspace was last shared under, if it ever was: an address on it is one of
 *  this workspace's apps, from any client. */
export function classifyLink(href: string, workspaceHost: string, shareDomain: string | null = null): LinkTarget {
  if (href.startsWith("/")) {
    // ``//host`` and ``/\host`` are other hosts to a browser, not paths.
    if (/^\/[/\\]/.test(href)) return { kind: "unroutable" };
    const path = filePathOf(href.split(/[?#]/, 1)[0]);
    return path === null ? { kind: "unroutable" } : { kind: "file", path };
  }
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    // Malformed but plainly a web link: the desktop app sends it to the browser rather than a window of its own.
    return WEB_URL_PREFIX.test(href) ? { kind: "external", url: href } : { kind: "unroutable" };
  }
  if (url.protocol === "file:") {
    // A file URL naming another machine is nothing this workspace can open.
    if (url.hostname !== "" && !LOCAL_HOSTNAMES.has(url.hostname.toLowerCase())) return { kind: "unroutable" };
    const path = filePathOf(url.pathname);
    return path === null ? { kind: "unroutable" } : { kind: "file", path };
  }
  if (WEB_SCHEMES.has(url.protocol) && !LOCAL_HOSTNAMES.has(url.hostname.toLowerCase())) {
    const shareAddress = shareAddressOf(url, workspaceHost, shareDomain);
    if (shareAddress !== null) return shareAddress;
  }
  if (isExternalUrl(url)) return { kind: "external", url: url.href };
  if (!WEB_SCHEMES.has(url.protocol)) return { kind: "unroutable" };
  return classifyLocalUrl(url, workspaceHost);
}
