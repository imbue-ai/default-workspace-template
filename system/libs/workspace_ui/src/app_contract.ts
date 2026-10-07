/**
 * The browser-side app contract (workspace app model, contracts.md section 10, extended for
 * the desktop interface by docs/system/blueprint/desktop-interface/contracts.md section 7):
 * the one postMessage module an app page imports to speak to the workspace shell that frames
 * it.
 *
 * Built as its own library entry (`vite.contract.config.ts`) into the shell's static output,
 * and served by every app at `/_static/app_contract.js` from its own origin: a page imports it
 * as a module, and a module import is a fetch without cookies, which the desktop client's
 * forwarder and the share gateway refuse across origins (the shell serves it too, with a
 * permissive CORS header, for the e2e stub pages). The chat document, which lives in this
 * same source tree, imports the source directly. It must therefore import nothing: the served
 * file has no other dependencies.
 *
 * Trust: a page accepts a message only from `window.parent` (a nested third-party frame can
 * post here but can never satisfy that identity), and sends only to `window.parent`. The
 * target origin is `*` for the reason the Imbue Studio embed contract gives: the workspace's own
 * `frame-ancestors` policy means only a workspace-family document can frame this page at all.
 * Unknown types are ignored and shipped types never change meaning; the contract evolves by
 * adding types.
 *
 * A framed page's link clicks follow one rule, so no app carries link code of its own
 * (``followLinkClick``): a link to the page's own origin navigates the page, or opens a window
 * of its app for a new-window click; an external link opens in a new browsing context, which
 * Imbue Studio sends to the user's browser; any other link (a local URL, another app's address,
 * a ``file:`` URL) goes to the shell, which opens it where it belongs.
 */

/** Shell to app: sent after every `load` of the frame; says which window, desktop, path, and client this page is in. */
export const SHELL_HANDSHAKE = "shell:handshake";
/** Shell to app: the window became visible in this client. */
export const SHELL_SHOWN = "shell:shown";
/** Shell to app: the window stopped being visible in this client. */
export const SHELL_HIDDEN = "shell:hidden";
/** Shell to app: the close chord fired while this window was focused. */
export const SHELL_CLOSE_REQUEST = "shell:close-request";
/** Shell to app: the window's path changed elsewhere; the page should show that path in place. */
export const SHELL_NAVIGATE = "shell:navigate";
/** App to shell: what this page can do, sent once on connect. */
export const SHELL_CAPABILITIES = "shell:capabilities";
/** App to shell: the page received focus; the shell raises its window. */
export const SHELL_FOCUSED = "shell:focused";
/** App to shell: the page reports where it is and what it is called. */
export const SHELL_LOCATION = "shell:location";
/** App to shell: open another page of this app beside this one. */
export const SHELL_OPEN = "shell:open";
/** App to shell: start something with a text (launcher-and-getting-started plan section 3.7): the shell runs its
 *  launcher's primary text action with it, so a page never names the app that takes it. */
export const SHELL_START_WITH_TEXT = "shell:start-with-text";
/** App to shell: draft a text into a chat, unsent (element-reference-menu plan section 5): the shell runs the
 *  pinned app's draft launch path with it, as its own "Design your own..." does, so a page never names the app. */
export const SHELL_DRAFT_TEXT = "shell:draft-text";
/** App to shell: a message for whichever apps registered its type (``open:file``, ...; desktop-interface
 *  contracts.md section 5.6), so a page says what it wants done without naming the app that does it. */
export const SHELL_MESSAGE = "shell:message";
/** App to shell: open a link that is neither the page's own nor external -- a local URL, one of the workspace's app
 *  addresses (another app's page) or another workspace's, a ``file:`` URL -- which only the shell can put on screen:
 *  it opens the app's window there (for a local URL, the app registered at its port, else the workspace's browser),
 *  the file in the File Viewer, or says why it cannot. */
export const SHELL_OPEN_LINK = "shell:open-link";

/** The bare host names of this machine (a ``*.localhost`` host is local too). */
export const LOCAL_HOSTNAMES: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** Whether ``url`` leaves this machine, so it opens in the user's own browser: exactly the rule Imbue Studio's
 *  ``isExternalUrl`` applies to a popup (``link-externality-vectors.json`` keeps the two in step). */
export function isExternalUrl(url: URL): boolean {
  if (url.protocol === "mailto:" || url.protocol === "tel:") return true;
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  const host = url.hostname.toLowerCase();
  return !LOCAL_HOSTNAMES.has(host) && !host.endsWith(".localhost");
}

/**
 * What the shell says about the frame it created: the client, the window, its desktop, the app
 * the window belongs to, and the path the window is at (desktop-interface contracts.md section
 * 7). The client id is required; a field the shell does not send, a page reads as "".
 */
export interface ShellHandshake {
  clientId: string;
  windowId: string;
  desktopId: string;
  app: string;
  path: string;
}

/**
 * What a page can do beyond the base contract. A page that handles `shell:navigate` in place
 * declares `navigation: true` and gives `onNavigate`; a shell then asks it to move rather than
 * reloading its frame. A page that owns the close chord (a browser closing one of its own
 * tabs) declares `closeChord: true` and gives `onCloseRequest`; a shell then only sends
 * `shell:close-request` and leaves the window open.
 */
export interface ShellCapabilities {
  navigation: boolean;
  closeChord: boolean;
}

/** What an open does when a page of this app at the same path is already showing. */
export type OpenIfPresent = "focus" | "new";

export interface ShellConnectionHandlers {
  onHandshake?: (handshake: ShellHandshake) => void;
  onShown?: () => void;
  onHidden?: () => void;
  onCloseRequest?: () => void;
  onNavigate?: (path: string) => void;
  capabilities?: ShellCapabilities;
}

export interface ShellConnection {
  /** Whether a shell frames this page at all; a top-level visit has no shell to talk to. */
  readonly isFramed: boolean;
  /** Tell the shell this page received focus. */
  focused(): void;
  /** Report where this page is now (a path under the app's origin) and what it is called. */
  location(path: string, title: string): void;
  /** Ask the shell to open a page of this app at a path beside this one. */
  openPath(path: string, ifPresent: OpenIfPresent): void;
  /** Ask the shell to start something with ``text``: its launcher's primary text action (a new chat on a stock machine). */
  startWithText(text: string): void;
  /** Ask the shell to draft ``text`` into a chat's composer, unsent (the chat on screen on a stock machine). */
  draftText(text: string): void;
  /** Send the shell a message for the apps registered for ``type``, with ``fields`` as its own fields. */
  sendMessage(type: string, fields: Readonly<Record<string, unknown>>): void;
  /** Ask the shell to open a local URL (as the window of the app at its port, else in the workspace's browser), an
   *  address of the workspace's apps (or another workspace's) as a window, or a ``file:`` URL in the File Viewer. */
  openLink(url: string): void;
  /** Stop listening to the shell. */
  disconnect(): void;
}

/** Raised when a page's handlers and its declared capabilities disagree. */
export class ShellContractError extends Error {}

const DEFAULT_CAPABILITIES: ShellCapabilities = { navigation: false, closeChord: false };

function optionalString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readHandshake(data: Record<string, unknown>): ShellHandshake | null {
  const { clientId, windowId, desktopId, app, path } = data;
  if (typeof clientId !== "string" || clientId === "") return null;
  return {
    clientId,
    windowId: optionalString(windowId),
    desktopId: optionalString(desktopId),
    app: optionalString(app),
    path: optionalString(path),
  };
}

function checkedCapabilities(handlers: ShellConnectionHandlers): ShellCapabilities {
  const capabilities = handlers.capabilities ?? DEFAULT_CAPABILITIES;
  const hasNavigateHandler = handlers.onNavigate !== undefined;
  if (hasNavigateHandler !== capabilities.navigation) {
    throw new ShellContractError(
      "a page that handles shell:navigate declares capabilities.navigation: true, and one that declares it gives onNavigate",
    );
  }
  if (capabilities.closeChord && handlers.onCloseRequest === undefined) {
    throw new ShellContractError("a page that declares capabilities.closeChord: true gives onCloseRequest");
  }
  return capabilities;
}

/** The schemes a framed page hands on when a link to them is clicked; any other (``javascript:``, ``blob:``,
 *  ``data:``) is left to the page. */
const HANDED_ON_SCHEMES: ReadonlySet<string> = new Set(["http:", "https:", "file:", "mailto:", "tel:"]);
const PRIMARY_BUTTON = 0;
const MIDDLE_BUTTON = 1;

/** The link a click landed in, if any (through shadow roots too). */
function clickedLink(event: MouseEvent): HTMLAnchorElement | HTMLAreaElement | null {
  for (const target of event.composedPath()) {
    if ((target instanceof HTMLAnchorElement || target instanceof HTMLAreaElement) && target.hasAttribute("href")) {
      return target;
    }
  }
  return null;
}

/** Whether a click asks for another window rather than this page: a middle or modified click, or a link that names
 *  another browsing context (``_top`` and ``_parent`` included, which would take the shell's page). */
function isNewWindowClick(event: MouseEvent, link: HTMLAnchorElement | HTMLAreaElement): boolean {
  const target = link.target.toLowerCase();
  return (
    event.button === MIDDLE_BUTTON ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    (target !== "" && target !== "_self")
  );
}

/** Follow a framed page's link click by the contract's rule (see the module docs); a click the page already handled
 *  (``defaultPrevented``), a download link, or a link to a scheme the rule does not hand on is left alone. */
function followLinkClick(
  event: MouseEvent,
  view: Window,
  send: (type: string, payload: Record<string, unknown>) => void,
): void {
  if (event.defaultPrevented) return;
  if (event.button !== (event.type === "auxclick" ? MIDDLE_BUTTON : PRIMARY_BUTTON)) return;
  const link = clickedLink(event);
  if (link === null || link.hasAttribute("download")) return;
  let url: URL;
  try {
    url = new URL(link.href);
  } catch {
    return;
  }
  if (!HANDED_ON_SCHEMES.has(url.protocol)) return;
  const isNewWindow = isNewWindowClick(event, link);
  if (url.origin === view.location.origin) {
    if (!isNewWindow) return;
    event.preventDefault();
    send(SHELL_OPEN, { path: `${url.pathname}${url.search}`, ifPresent: "focus" });
    return;
  }
  event.preventDefault();
  if (isExternalUrl(url)) {
    view.open(url.href, "_blank", "noopener");
    return;
  }
  send(SHELL_OPEN_LINK, { url: url.href });
}

/**
 * Connect this page to the shell that frames it. Safe to call on a top-level page: nothing
 * arrives, and every send is a no-op, so an app behaves the same visited directly. A framed
 * page's link clicks follow the contract's rule from here on (see the module docs).
 */
export function connectToShell(handlers: ShellConnectionHandlers): ShellConnection {
  const capabilities = checkedCapabilities(handlers);
  const boundWindow = window;
  const isFramed = boundWindow.parent !== boundWindow;

  function onMessage(event: MessageEvent): void {
    if (!isFramed || event.source !== boundWindow.parent) return;
    const data: unknown = event.data;
    if (data === null || typeof data !== "object") return;
    const message = data as Record<string, unknown>;
    switch (message.type) {
      case SHELL_HANDSHAKE: {
        const handshake = readHandshake(message);
        if (handshake !== null) handlers.onHandshake?.(handshake);
        return;
      }
      case SHELL_SHOWN:
        handlers.onShown?.();
        return;
      case SHELL_HIDDEN:
        handlers.onHidden?.();
        return;
      case SHELL_CLOSE_REQUEST:
        handlers.onCloseRequest?.();
        return;
      case SHELL_NAVIGATE: {
        const path = message.path;
        if (typeof path === "string") handlers.onNavigate?.(path);
        return;
      }
      default:
        return;
    }
  }

  function send(type: string, payload: Record<string, unknown>): void {
    if (!isFramed) return;
    boundWindow.parent.postMessage({ type, ...payload }, "*");
  }

  // On the window, the last stop of a click's bubbling, so the page's own handlers see it first.
  const onLinkClick = (event: MouseEvent): void => followLinkClick(event, boundWindow, send);

  boundWindow.addEventListener("message", onMessage);
  if (isFramed) {
    boundWindow.addEventListener("click", onLinkClick);
    boundWindow.addEventListener("auxclick", onLinkClick);
  }
  send(SHELL_CAPABILITIES, { navigation: capabilities.navigation, closeChord: capabilities.closeChord });
  return {
    isFramed,
    focused: () => send(SHELL_FOCUSED, {}),
    location: (path: string, title: string) => send(SHELL_LOCATION, { path, title }),
    openPath: (path: string, ifPresent: OpenIfPresent) => send(SHELL_OPEN, { path, ifPresent }),
    startWithText: (text: string) => send(SHELL_START_WITH_TEXT, { text }),
    draftText: (text: string) => send(SHELL_DRAFT_TEXT, { text }),
    sendMessage: (type: string, fields: Readonly<Record<string, unknown>>) =>
      send(SHELL_MESSAGE, { message: { ...fields, type } }),
    openLink: (url: string) => send(SHELL_OPEN_LINK, { url }),
    disconnect: () => {
      boundWindow.removeEventListener("message", onMessage);
      boundWindow.removeEventListener("click", onLinkClick);
      boundWindow.removeEventListener("auxclick", onLinkClick);
    },
  };
}
