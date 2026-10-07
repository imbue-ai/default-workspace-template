/**
 * Where a link clicked inside the workspace goes (the workspace link routing plan): one classifier shared by every
 * place a link is followed, so a link means the same thing wherever it is.
 *
 * - A web link off this machine (and ``mailto:``, ``tel:``) is **external**: it opens in the user's own browser. The
 *   rule is exactly the Imbue Studio desktop app's ``isExternalUrl``, which decides the same thing for a popup, and
 *   a shared fixture keeps the two in step.
 * - An absolute path is a **file** of the workspace, opened in the File Viewer (``open:file``).
 * - A URL on a local host (``localhost``, ``127.0.0.1``, ``[::1]``, ``*.localhost``) is a **local URL**, which the
 *   shell opens (``shell:open-link``): on a bare host name, as the window of the app registered at its port, else
 *   in the workspace's browser (``open:url``). That is unless it is the address of an app of a workspace: one of
 *   this workspace's apps (an **app address**, opened as that app's window: by the page itself for its own app, by
 *   the shell through ``shell:open-link`` for another) or another workspace's (which the shell refuses).
 * - Anything else (a relative path, a fragment, another scheme) is **unroutable**.
 *
 * This module touches no message primitive: it acts through the page's app-contract connection.
 */

import type { OpenIfPresent } from "./app_contract";
import { hasWorkspaceCoordinate, workspaceHostCoordinate } from "./origin";

/** What a link is, as the workspace routes it. */
export type LinkTarget =
  | { readonly kind: "external"; readonly url: string }
  | { readonly kind: "local-url"; readonly url: string }
  | { readonly kind: "app-address"; readonly label: string; readonly path: string; readonly url: string }
  | { readonly kind: "other-workspace"; readonly url: string }
  | { readonly kind: "file"; readonly path: string }
  | { readonly kind: "unroutable" };

/** The message types a link becomes (desktop-interface contracts.md section 5.6): ``open:file``, which a routed file
 *  link sends as a ``shell:message``, and ``open:url``, which the shell sends for a local URL no app is registered
 *  at. */
export const OPEN_FILE_MESSAGE = "open:file";
export const OPEN_URL_MESSAGE = "open:url";

/** The bare host names of this machine (a ``*.localhost`` host is local too). */
export const LOCAL_HOSTNAMES: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);
const LOCAL_HOSTNAME_SUFFIX = ".localhost";
const EXTERNAL_SCHEMES: ReadonlySet<string> = new Set(["mailto:", "tel:"]);
const WEB_SCHEMES: ReadonlySet<string> = new Set(["http:", "https:"]);
const WEB_URL_PREFIX = /^https?:\/\//i;

/** Whether a URL's hostname is on this machine, by the rule Imbue Studio's ``isExternalUrl`` applies. */
function isLocalHostname(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return LOCAL_HOSTNAMES.has(host) || host.endsWith(LOCAL_HOSTNAME_SUFFIX);
}

/** The file an absolute-path link names: its path decoded, with no query or fragment and no trailing slash (but
 *  the root's), so one file or folder has one spelling; null when a segment does not decode. */
function filePathOf(href: string): string | null {
  const path = href.split(/[?#]/, 1)[0];
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

/** A local URL, as this workspace sees it: one of its own apps' addresses, another workspace's, or neither. */
function classifyLocalUrl(url: URL, workspaceHost: string): LinkTarget {
  const host = url.host.toLowerCase();
  if (!hasWorkspaceCoordinate(host)) return { kind: "local-url", url: url.href };
  const coordinate = workspaceHostCoordinate(host);
  if (coordinate !== workspaceHostCoordinate(workspaceHost.toLowerCase())) {
    return { kind: "other-workspace", url: url.href };
  }
  const appLabels = host.slice(0, host.length - coordinate.length).replace(/\.$/, "");
  if (appLabels === "") return { kind: "unroutable" };
  return { kind: "app-address", label: appLabels.split(".")[0], path: `${url.pathname}${url.search}`, url: url.href };
}

/** What ``href`` (a link's attribute as written, not resolved against the page) is, for a page served under
 *  ``workspaceHost`` (a host of this workspace, such as the page's own ``location.host``). */
export function classifyLink(href: string, workspaceHost: string): LinkTarget {
  if (href.startsWith("/")) {
    // ``//host`` and ``/\host`` are other hosts to a browser, not paths.
    if (/^\/[/\\]/.test(href)) return { kind: "unroutable" };
    const path = filePathOf(href);
    return path === null ? { kind: "unroutable" } : { kind: "file", path };
  }
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    // Malformed but plainly a web link: the desktop app sends it to the browser rather than a window of its own.
    return WEB_URL_PREFIX.test(href) ? { kind: "external", url: href } : { kind: "unroutable" };
  }
  if (EXTERNAL_SCHEMES.has(url.protocol)) return { kind: "external", url: url.href };
  if (!WEB_SCHEMES.has(url.protocol)) return { kind: "unroutable" };
  if (!isLocalHostname(url.hostname)) return { kind: "external", url: url.href };
  return classifyLocalUrl(url, workspaceHost);
}

/** What routing a link acts through: the page's connection to the shell, and the page's own window. */
export interface LinkRoutingContext {
  /** Whether a shell frames the page; unframed, a file downloads and a local URL opens in a new browser tab. */
  readonly isFramed: boolean;
  openPath(path: string, ifPresent: OpenIfPresent): void;
  sendMessage(type: string, fields: Readonly<Record<string, unknown>>): void;
  /** Ask the shell to open a local URL, or a workspace app address that is not the page's own
   *  (``shell:open-link``). */
  openLink(url: string): void;
  /** The page's own host, which says which workspace (and which app) it is in. */
  readonly pageHost: string;
  /** Open a URL in a new browser tab or window: Imbue Studio sends an external one to the user's browser, and
   *  hands one of a workspace's addresses back to the workspace. */
  openInNewTab(url: string): void;
  /** Download an absolute path from the page's own origin. */
  download(path: string): void;
}

/** The first hostname label of a host: an app page's own origin label. */
function firstLabel(host: string): string {
  return host.toLowerCase().split(".")[0];
}

/** Follow ``href`` as a click on it would: send the shell the message that opens it inside the workspace, open it
 *  in the browser, or take the unframed fallbacks. Answers whether the link was routed (an unroutable one is not). */
export function routeLink(href: string, context: LinkRoutingContext): boolean {
  const target = classifyLink(href, context.pageHost);
  switch (target.kind) {
    case "unroutable":
      return false;
    case "file":
      if (context.isFramed) context.sendMessage(OPEN_FILE_MESSAGE, { path: target.path });
      else context.download(href);
      return true;
    case "local-url":
      // The shell knows which app, if any, serves the URL's port; only it can tell an app's window from a page.
      if (context.isFramed) context.openLink(target.url);
      else context.openInNewTab(target.url);
      return true;
    case "app-address":
      // A page opens pages of its own app itself; any other app's window is the shell's to open.
      if (!context.isFramed) context.openInNewTab(target.url);
      else if (target.label === firstLabel(context.pageHost)) context.openPath(target.path, "focus");
      else context.openLink(target.url);
      return true;
    case "other-workspace":
      // The shell refuses it with a notice; a plain browser tab is all an unframed page can offer.
      if (context.isFramed) context.openLink(target.url);
      else context.openInNewTab(target.url);
      return true;
    case "external":
      context.openInNewTab(target.url);
      return true;
  }
}

/** Marks the root ``installLinkRouting`` routes the clicks of, naming its selector. A DOM mark, so the element menu
 *  served to every app (a bundle of its own) sees which links the page routes. */
export const LINK_ROUTING_SELECTOR_ATTR = "data-link-routing-selector";

/** Whether a click on ``anchor`` goes through ``installLinkRouting``. */
function isClickRouted(anchor: HTMLAnchorElement): boolean {
  const root = anchor.closest(`[${LINK_ROUTING_SELECTOR_ATTR}]`);
  const selector = root?.getAttribute(LINK_ROUTING_SELECTOR_ATTR) ?? null;
  return selector !== null && anchor.matches(selector);
}

/** Follow a link element as a click on it would go: the link as written, or the page address it resolves to for
 *  one written relative to the page (an app page's ``details.html``). An absolute path names a file only in a link
 *  the page routes (a chat message's); in any other link it is a page of the page's own app. Answers whether it
 *  was routed. */
export function routeLinkElement(anchor: HTMLAnchorElement, context: LinkRoutingContext): boolean {
  const href = anchor.getAttribute("href") ?? "";
  const isOwnAppPath = href.startsWith("/") && !isClickRouted(anchor);
  if (!isOwnAppPath && routeLink(href, context)) return true;
  return WEB_SCHEMES.has(anchor.protocol) && routeLink(anchor.href, context);
}

/** The routing context of a page from its window and its shell connection. */
export function pageLinkRoutingContext(
  view: Window,
  connection: Pick<LinkRoutingContext, "isFramed" | "openPath" | "sendMessage" | "openLink">,
): LinkRoutingContext {
  return {
    isFramed: connection.isFramed,
    openPath: (path, ifPresent) => connection.openPath(path, ifPresent),
    sendMessage: (type, fields) => connection.sendMessage(type, fields),
    openLink: (url) => connection.openLink(url),
    pageHost: view.location.host,
    openInNewTab: (url) => void view.open(url, "_blank", "noopener"),
    download: (path) => {
      const anchor = view.document.createElement("a");
      anchor.href = path;
      anchor.download = "";
      anchor.click();
    },
  };
}

/** The middle button, whose click is an ``auxclick``. */
const MIDDLE_BUTTON = 1;

/** Route every click on a link matching ``selector`` inside ``root`` (a plain, modified, or middle click alike)
 *  through ``routeLink``, so the page never navigates to it and no bare window opens. An unroutable link is left
 *  to the page. Answers a function that removes the listeners. */
export function installLinkRouting(root: Element, selector: string, context: LinkRoutingContext): () => void {
  const onClick = (event: MouseEvent): void => {
    if (event.defaultPrevented) return;
    if (event.type === "auxclick" && event.button !== MIDDLE_BUTTON) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    const anchor = target.closest(selector);
    if (anchor === null || !root.contains(anchor)) return;
    const href = anchor.getAttribute("href");
    if (href === null) return;
    if (routeLink(href, context)) event.preventDefault();
  };
  root.addEventListener("click", onClick as EventListener);
  root.addEventListener("auxclick", onClick as EventListener);
  root.setAttribute(LINK_ROUTING_SELECTOR_ATTR, selector);
  return () => {
    root.removeEventListener("click", onClick as EventListener);
    root.removeEventListener("auxclick", onClick as EventListener);
    root.removeAttribute(LINK_ROUTING_SELECTOR_ATTR);
  };
}
