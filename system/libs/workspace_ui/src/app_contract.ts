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
 * A page knows a shell frames it once a handshake arrives from its parent (``hasShell``); a page
 * framed by anything else, such as the chat root opened on its own, has no shell to ask, so its
 * links and popups stay the browser's.
 *
 * Once a shell frames a page, its link clicks follow one rule, so no app carries link code of its own
 * (``followLinkClick``): a link to the page's own origin navigates the page, or opens a window
 * of its app for a new-window click; any other link (a local URL, another app's address, a
 * ``file:`` URL, an external web, ``mailto:``, ``tel:`` or custom-scheme link) goes to the shell,
 * which opens it where it belongs: an external one in the app registered for it, else outside the
 * workspace.
 *
 * Its script popups (``window.open``) follow the same rule for what the workspace
 * opens itself (``popupHandedOn``): a page of its own app opens a window of the app, and a local
 * URL, another app's address (on the page's own share domain too) or a ``file:`` URL goes to the
 * shell; the call answers null, as a blocked popup does. A popup to any other site, a blank window,
 * and other schemes stay the browser's, since a script that opened one may need the window back.
 * A call that names a frame of the page stays the browser's, and one that names ``_self`` follows
 * the rule for a plain click: the page's own origin navigates the page, any other link goes to the
 * shell.
 */

/** Shell to app: sent after every `load` of the frame; says which window, desktop, path, and client this page is in,
 *  and whether "Report a bug" can work here. */
export const SHELL_HANDSHAKE = "shell:handshake";
/** Shell to app: the window became visible in this client. */
export const SHELL_SHOWN = "shell:shown";
/** Shell to app: the window stopped being visible in this client. */
export const SHELL_HIDDEN = "shell:hidden";
/** Shell to app: the close chord fired while this window was focused. */
export const SHELL_CLOSE_REQUEST = "shell:close-request";
/** Shell to app: the theme the shell wears, the desktop on screen's (or one Desktop settings is previewing), its
 *  revision, and whether it is such a preview (docs/system/blueprint/workspace-themes/, section 5.3), sent after the
 *  handshake and on every change; the page wears it, and remembers it unless it is a preview
 *  (`themes/themeClient.ts`). */
export const SHELL_THEME = "shell:theme";
/** Shell to app: somebody changed a keybinding; the page re-fetches its own backend's
 *  `/api/keybindings/me` (no payload: each app resolves for its own actions and identity). */
export const SHELL_KEYBINDINGS_CHANGED = "shell:keybindings-changed";
/** Shell to app: the window's path changed elsewhere; the page should show that path in place. */
export const SHELL_NAVIGATE = "shell:navigate";
/** App to shell: what this page can do, sent once on connect. */
export const SHELL_CAPABILITIES = "shell:capabilities";
/** App to shell: the page received focus; the shell raises its window. */
export const SHELL_FOCUSED = "shell:focused";
/** App to shell: a person touched the page (a pointer press or a key), throttled. The shell's presence heartbeat
 *  reads activity off it, since input inside an app's frame never bubbles to the shell's own window. */
export const SHELL_INPUT = "shell:input";
/** App to shell: the page reports where it is and what it is called. */
export const SHELL_LOCATION = "shell:location";
/** App to shell: open another page of this app beside this one. */
export const SHELL_OPEN = "shell:open";
/** App to shell: start something with a text (launcher-and-getting-started plan section 3.7): the shell runs its
 *  launcher's primary text action with it, so a page never names the app that takes it. */
export const SHELL_START_WITH_TEXT = "shell:start-with-text";
/** App to shell: start something new with a text (``{text, requestId}``): the shell runs its launcher's secondary text
 *  action with it (a new chat on a stock machine, where the primary one sends to the main chat), else the primary one
 *  where there is no second, and answers with ``shell:start-new-with-text-result``. */
export const SHELL_START_NEW_WITH_TEXT = "shell:start-new-with-text";
/** Shell to app: how a page's ``shell:start-new-with-text`` went (``{requestId, isOpened: true}``, or
 *  ``{requestId, isOpened: false, failure: {message, details, isReportable}}``), once the window it opened is on
 *  the desktop or the launch failed. */
export const SHELL_START_NEW_WITH_TEXT_RESULT = "shell:start-new-with-text-result";
/** App to shell: draft a text into a chat, unsent (element-reference-menu plan section 5): the shell runs the
 *  pinned app's draft launch path with it, as its own "Design your own..." does, so a page never names the app. */
export const SHELL_DRAFT_TEXT = "shell:draft-text";
/** App to shell: a message for whichever apps registered its type (``open:file``, ...; desktop-interface
 *  contracts.md section 5.6), so a page says what it wants done without naming the app that does it. */
export const SHELL_MESSAGE = "shell:message";
/** App to shell: open a link that is not the page's own -- a local URL, one of the workspace's app addresses (another
 *  app's page) or another workspace's, a ``file:`` URL, an external web, ``mailto:``, ``tel:`` or custom-scheme link
 *  -- which only the shell can put where it belongs: it opens the app's window there (for a local URL, the app on whose
 *  ``<label>.localhost`` host or registered port it is, else the workspace's browser), the file in Files, an
 *  external link in the app registered for it or else outside the workspace, or says why it cannot. */
export const SHELL_OPEN_LINK = "shell:open-link";
/** App to shell: open Imbue Studio's bug report about a failure (``{message, details}``, a ``ReportedFailure``), as
 *  the shell's own "Report a bug" does. The shell acts on it only where its handshake said ``canReportBug``, since
 *  only the shell can reach the embedder that opens one. */
export const SHELL_REPORT_BUG = "shell:report-bug";

/** The bare host names of this machine (a ``*.localhost`` host is local too). */
export const LOCAL_HOSTNAMES: ReadonlySet<string> = new Set(["localhost", "127.0.0.1", "[::1]"]);

/** Whether ``url`` leaves this machine (a ``mailto:`` or ``tel:`` link included): exactly the rule Imbue Studio's
 *  ``isExternalUrl`` applies to a popup (``link-externality-vectors.json`` keeps the two in step). */
export function isExternalUrl(url: URL): boolean {
  if (url.protocol === "mailto:" || url.protocol === "tel:") return true;
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  const host = url.hostname.toLowerCase();
  return !LOCAL_HOSTNAMES.has(host) && !host.endsWith(".localhost");
}

/** Schemes a browser loads, or refuses, itself. */
const IN_BROWSER_SCHEMES: ReadonlySet<string> = new Set([
  "http:",
  "https:",
  "ws:",
  "wss:",
  "file:",
  "data:",
  "blob:",
  "about:",
  "javascript:",
  "filesystem:",
  "chrome:",
  "chrome-untrusted:",
  "chrome-extension:",
  "devtools:",
  "view-source:",
]);

/** Whether ``url`` is a custom-scheme link (``slack:``, ``vscode:``), which the app it belongs to opens unless an app
 *  of the workspace takes it: exactly Imbue Studio's ``isCustomSchemeUrl`` (``link-externality-vectors.json`` keeps
 *  the two in step). */
export function isCustomSchemeUrl(url: URL): boolean {
  return !IN_BROWSER_SCHEMES.has(url.protocol) && url.protocol !== "mailto:" && url.protocol !== "tel:";
}

/** A label that starts a workspace coordinate: ``host-<hex>`` (``agent-`` is
 *  the legacy spelling of the same coordinate), or a bare 32-hex label -- the
 *  share label leading a workspace-keyed share domain
 *  (``<share-label>.<user-hash>.<region>.<domain>``). App labels can never
 *  match either form: ``host-``/``agent-`` prefixes are reserved in
 *  forward_port.py, and a minted label is always ``<name>-<rand>`` (the hyphen
 *  plus non-hex name keeps it out of the bare 32-hex shape). */
const WORKSPACE_COORDINATE_LABEL = /^(?:(?:host|agent)-[a-f0-9]+|[a-f0-9]{32})$/i;

/** The workspace coordinate within ``host``: the first coordinate label and
 *  everything after it (``host-<hex>.localhost:8421`` locally,
 *  ``host-<hex>.<user>.<region>.<domain>`` on a legacy share,
 *  ``<share-label>.<user-hash>.<region>.<domain>`` on a workspace-keyed
 *  share), with any leading app label(s) stripped. Returns ``host``
 *  unchanged when it carries no coordinate label (a non-workspace host), so
 *  the derivation degrades safely. */
export function workspaceHostCoordinate(host: string): string {
  const labels = host.split(".");
  const coordinateIndex = labels.findIndex((label) => WORKSPACE_COORDINATE_LABEL.test(label));
  return coordinateIndex < 0 ? host : labels.slice(coordinateIndex).join(".");
}

/** Whether ``host`` is a workspace's: some hostname label of it starts a workspace coordinate. */
export function hasWorkspaceCoordinate(host: string): boolean {
  return host.split(".").some((label) => WORKSPACE_COORDINATE_LABEL.test(label));
}

/**
 * What the shell says about the frame it created: the client, the window, its desktop, the app
 * the window belongs to, the path the window is at, whether the shell can open Imbue Studio's bug report, whether
 * the Imbue Studio chrome around the shell opens its settings for the files shared from the user's computer
 * (desktop-interface contracts.md section 7), and whether the window is being viewed on somebody else's desktop.
 * The client id is required; a text field the shell does not send, a page reads as "", and a flag it does not
 * send, as false.
 */
export interface ShellHandshake {
  clientId: string;
  windowId: string;
  desktopId: string;
  app: string;
  path: string;
  canReportBug: boolean;
  opensLocalFiles: boolean;
  /** Whether the window is being looked at on somebody else's desktop (the multiplayer spec, section 8.3): the page
   *  is inert there, and one that attaches to something shared (a terminal's tmux session) attaches as a viewer. */
  isViewing: boolean;
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
  /** `isPreview`: the shell shows the theme without having saved it; a page wears it and does not remember it. */
  onTheme?: (theme: string, revision: string, isPreview: boolean) => void;
  /** Somebody changed a keybinding; re-fetch the resolved chords from this app's own backend. */
  onKeybindingsChanged?: () => void;
  capabilities?: ShellCapabilities;
}

export interface ShellConnection {
  /** Whether anything frames this page; a top-level visit has no shell to talk to. */
  readonly isFramed: boolean;
  /** Whether a shell frames this page: its handshake has arrived. */
  hasShell(): boolean;
  /** Tell the shell this page received focus. */
  focused(): void;
  /** Report where this page is now (a path under the app's origin) and what it is called. */
  location(path: string, title: string): void;
  /** Ask the shell to open a page of this app at a path beside this one. */
  openPath(path: string, ifPresent: OpenIfPresent): void;
  /** Ask the shell to start something with ``text``: its launcher's primary text action (the main chat on a stock
   *  machine). */
  startWithText(text: string): void;
  /** Ask the shell to start something new with ``text``: its launcher's secondary text action (a new chat on a stock
   *  machine). Settles on the shell's answer; as not answered at once without a shell, when a fresh handshake
   *  comes first (the shell started again and forgot the request), and on ``disconnect``, after which no answer is
   *  heard, so a later call settles at once too. */
  startNewWithText(text: string): Promise<StartNewOutcome>;
  /** Whether "Report a bug" would do anything: the shell's latest handshake said ``canReportBug``. */
  canReportBug(): boolean;
  /** Ask the shell to open Imbue Studio's bug report about ``failure``, which the report carries. */
  reportBug(failure: ReportedFailure): void;
  /** Ask the shell to draft ``text`` into a chat's composer, unsent (the chat on screen on a stock machine). */
  draftText(text: string): void;
  /** Send the shell a message for the apps registered for ``type``, with ``fields`` as its own fields. */
  sendMessage(type: string, fields: Readonly<Record<string, unknown>>): void;
  /** Ask the shell to open a local URL (as the window of the app on its ``<label>.localhost`` host or at its port, else
   *  in the workspace's browser), an address of the workspace's apps (or another workspace's) as a window, a ``file:``
   *  URL in Files, or an external web, ``mailto:``, ``tel:`` or custom-scheme link in the app registered for it, else
   *  outside the workspace. */
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
  const { clientId, windowId, desktopId, app, path, canReportBug, opensLocalFiles, isViewing } = data;
  if (typeof clientId !== "string" || clientId === "") return null;
  return {
    clientId,
    windowId: optionalString(windowId),
    desktopId: optionalString(desktopId),
    app: optionalString(app),
    path: optionalString(path),
    canReportBug: canReportBug === true,
    opensLocalFiles: opensLocalFiles === true,
    isViewing: isViewing === true,
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

function readTheme(message: Record<string, unknown>): { theme: string; revision: string; isPreview: boolean } | null {
  if (typeof message.theme !== "string") return null;
  return {
    theme: message.theme,
    revision: typeof message.revision === "string" ? message.revision : "",
    isPreview: message.isPreview === true,
  };
}

/**
 * Follow the shell's `shell:theme` without connecting to it: for the page kit, which runs beside whatever
 * `connectToShell` the page makes itself. Answers a function that stops following.
 */
export function followShellTheme(onTheme: (theme: string, revision: string, isPreview: boolean) => void): () => void {
  const boundWindow = window;
  if (boundWindow.parent === boundWindow) return () => undefined;

  function onMessage(event: MessageEvent): void {
    if (event.source !== boundWindow.parent) return;
    const data: unknown = event.data;
    if (data === null || typeof data !== "object") return;
    const message = data as Record<string, unknown>;
    if (message.type !== SHELL_THEME) return;
    const theme = readTheme(message);
    if (theme !== null) onTheme(theme.theme, theme.revision, theme.isPreview);
  }

  boundWindow.addEventListener("message", onMessage);
  return () => boundWindow.removeEventListener("message", onMessage);
}

/** The browser's own schemes a framed page hands on when a link to them is clicked, as it does every custom-scheme
 *  link; any other (``javascript:``, ``blob:``, ``data:``) is left to the page. */
const HANDED_ON_SCHEMES: ReadonlySet<string> = new Set(["http:", "https:", "file:", "mailto:", "tel:"]);

function isHandedOn(url: URL): boolean {
  return HANDED_ON_SCHEMES.has(url.protocol) || isCustomSchemeUrl(url);
}
const PRIMARY_BUTTON = 0;
const MIDDLE_BUTTON = 1;

/** How often at most the page reports input (``shell:input``): activity is measured in minutes, so a report every
 *  20 seconds loses nothing and keeps the message traffic negligible. The shell's page driver throttles its focus
 *  fallback for a page that binds no contract by the same interval. */
export const INPUT_REPORT_INTERVAL_MS = 20_000;

/** The link a click landed in, if any (through shadow roots too). */
function clickedLink(event: MouseEvent): HTMLAnchorElement | HTMLAreaElement | null {
  for (const target of event.composedPath()) {
    if ((target instanceof HTMLAnchorElement || target instanceof HTMLAreaElement) && target.hasAttribute("href")) {
      return target;
    }
  }
  return null;
}

function isModifiedClick(event: MouseEvent): boolean {
  return event.button === MIDDLE_BUTTON || event.metaKey || event.ctrlKey || event.shiftKey;
}

/** Whether a click asks for another window rather than this page: a middle or modified click, or a link that names
 *  another browsing context (``_top`` and ``_parent`` included, which would take the shell's page). */
function isNewWindowClick(event: MouseEvent, link: HTMLAnchorElement | HTMLAreaElement): boolean {
  const target = link.target.toLowerCase();
  return isModifiedClick(event) || (target !== "" && target !== "_self");
}

/** Whether ``name`` (a link's ``target``, or a ``window.open`` call's) names a frame of the page itself (an
 *  ``<iframe name>``), which a plain click or the call loads the URL into. */
function namesFrameOfPage(name: string, view: Window): boolean {
  if (name === "" || name.startsWith("_")) return false;
  return Array.from(view.document.querySelectorAll("iframe[name], frame[name]")).some(
    (frame) => frame.getAttribute("name") === name,
  );
}

/** Follow a framed page's link click by the contract's rule (see the module docs); a click the page already handled
 *  (``defaultPrevented``), a download link, a plain click on a link into a frame of the page, or a link to a scheme
 *  the rule does not hand on is left alone. */
function followLinkClick(
  event: MouseEvent,
  view: Window,
  send: (type: string, payload: Record<string, unknown>) => void,
): void {
  if (event.defaultPrevented) return;
  if (event.button !== (event.type === "auxclick" ? MIDDLE_BUTTON : PRIMARY_BUTTON)) return;
  const link = clickedLink(event);
  if (link === null || link.hasAttribute("download")) return;
  if (!isModifiedClick(event) && namesFrameOfPage(link.target, view)) return;
  let url: URL;
  try {
    url = new URL(link.href);
  } catch {
    return;
  }
  if (!isHandedOn(url)) return;
  const isNewWindow = isNewWindowClick(event, link);
  if (url.origin === view.location.origin) {
    if (!isNewWindow) return;
    event.preventDefault();
    send(SHELL_OPEN, { path: `${url.pathname}${url.search}`, ifPresent: "focus" });
    return;
  }
  event.preventDefault();
  send(SHELL_OPEN_LINK, { url: url.href });
}

/** What the shell is sent for a ``window.open(url, windowName)`` a framed page's script calls (see the module docs),
 *  or null for one the browser's own ``window.open`` opens. */
function popupHandedOn(
  url: unknown,
  windowName: unknown,
  view: Window,
): { readonly type: string; readonly payload: Record<string, unknown> } | null {
  if (url === undefined || url === null || String(url) === "") return null;
  const name = windowName === undefined || windowName === null ? "" : String(windowName);
  if (namesFrameOfPage(name, view)) return null;
  let target: URL;
  try {
    target = new URL(String(url), view.location.href);
  } catch {
    return null;
  }
  const openLink = { type: SHELL_OPEN_LINK, payload: { url: target.href } };
  if (name.toLowerCase() === "_self") {
    return isHandedOn(target) && target.origin !== view.location.origin ? openLink : null;
  }
  if (target.protocol === "file:") return openLink;
  if (target.protocol !== "http:" && target.protocol !== "https:") return null;
  if (target.origin === view.location.origin) {
    return { type: SHELL_OPEN, payload: { path: `${target.pathname}${target.search}`, ifPresent: "focus" } };
  }
  const host = target.host.toLowerCase();
  const isOnPageCoordinate =
    hasWorkspaceCoordinate(host) &&
    workspaceHostCoordinate(host) === workspaceHostCoordinate(view.location.host.toLowerCase());
  return !isExternalUrl(target) || isOnPageCoordinate ? openLink : null;
}

/**
 * Connect this page to the shell that frames it. Safe to call on a top-level page: nothing
 * arrives, and every send is a no-op, so an app behaves the same visited directly. Once a
 * shell's handshake arrives, the page's link clicks and script popups follow the contract's rule
 * (see the module docs).
 */
export function connectToShell(handlers: ShellConnectionHandlers): ShellConnection {
  const capabilities = checkedCapabilities(handlers);
  const boundWindow = window;
  const isFramed = boundWindow.parent !== boundWindow;
  let hasShell = false;
  let canReportBug = false;
  let isDisconnected = false;
  const pendingStarts = new Map<string, (outcome: StartNewOutcome) => void>();

  function settlePendingStartsAsNotAnswered(): void {
    const settles = Array.from(pendingStarts.values());
    pendingStarts.clear();
    for (const settle of settles) settle(START_NOT_ANSWERED);
  }

  function startNewWithText(text: string): Promise<StartNewOutcome> {
    if (!hasShell || isDisconnected) return Promise.resolve(START_NOT_ANSWERED);
    const requestId = newRequestId();
    const outcome = new Promise<StartNewOutcome>((resolve) => pendingStarts.set(requestId, resolve));
    send(SHELL_START_NEW_WITH_TEXT, { text, requestId });
    return outcome;
  }

  function onMessage(event: MessageEvent): void {
    if (!isFramed || event.source !== boundWindow.parent) return;
    const data: unknown = event.data;
    if (data === null || typeof data !== "object") return;
    const message = data as Record<string, unknown>;
    switch (message.type) {
      case SHELL_HANDSHAKE: {
        const handshake = readHandshake(message);
        if (handshake === null) return;
        hasShell = true;
        canReportBug = handshake.canReportBug;
        settlePendingStartsAsNotAnswered();
        handlers.onHandshake?.(handshake);
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
      case SHELL_THEME: {
        const theme = readTheme(message);
        if (theme !== null) handlers.onTheme?.(theme.theme, theme.revision, theme.isPreview);
        return;
      }
      case SHELL_KEYBINDINGS_CHANGED:
        handlers.onKeybindingsChanged?.();
        return;
      case SHELL_NAVIGATE: {
        const path = message.path;
        if (typeof path === "string") handlers.onNavigate?.(path);
        return;
      }
      case SHELL_START_NEW_WITH_TEXT_RESULT: {
        const requestId = message.requestId;
        if (typeof requestId !== "string") return;
        const settle = pendingStarts.get(requestId);
        if (settle === undefined) return;
        pendingStarts.delete(requestId);
        settle(readStartNewOutcome(message));
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

  // Input inside the page never bubbles to the shell's window, so the page reports it (the multiplayer spec,
  // section 4.3): at most one message per interval, which is plenty for an active/idle call measured in minutes.
  let lastInputReportAt = 0;
  const onInput = (): void => {
    const now = Date.now();
    if (now - lastInputReportAt < INPUT_REPORT_INTERVAL_MS) return;
    lastInputReportAt = now;
    send(SHELL_INPUT, {});
  };

  // On the window, the last stop of a click's bubbling, so the page's own handlers see it first.
  const onLinkClick = (event: MouseEvent): void => {
    if (hasShell) followLinkClick(event, boundWindow, send);
  };
  const nativeOpen = boundWindow.open;
  const routedOpen: typeof boundWindow.open = new Proxy(nativeOpen, {
    apply(target, thisArg, args: unknown[]) {
      const handedOn = hasShell ? popupHandedOn(args[0], args[1], boundWindow) : null;
      if (handedOn === null) return Reflect.apply(target, thisArg, args);
      send(handedOn.type, handedOn.payload);
      return null;
    },
  });

  boundWindow.addEventListener("message", onMessage);
  if (isFramed) {
    boundWindow.addEventListener("click", onLinkClick);
    boundWindow.addEventListener("auxclick", onLinkClick);
    for (const type of ["pointerdown", "keydown"]) {
      boundWindow.addEventListener(type, onInput, { capture: true, passive: true });
    }
    boundWindow.open = routedOpen;
  }
  send(SHELL_CAPABILITIES, { navigation: capabilities.navigation, closeChord: capabilities.closeChord });
  return {
    isFramed,
    hasShell: () => hasShell,
    focused: () => send(SHELL_FOCUSED, {}),
    location: (path: string, title: string) => send(SHELL_LOCATION, { path, title }),
    openPath: (path: string, ifPresent: OpenIfPresent) => send(SHELL_OPEN, { path, ifPresent }),
    startWithText: (text: string) => send(SHELL_START_WITH_TEXT, { text }),
    startNewWithText,
    canReportBug: () => canReportBug,
    reportBug: (failure: ReportedFailure) =>
      send(SHELL_REPORT_BUG, { message: failure.message, details: failure.details }),
    draftText: (text: string) => send(SHELL_DRAFT_TEXT, { text }),
    sendMessage: (type: string, fields: Readonly<Record<string, unknown>>) =>
      send(SHELL_MESSAGE, { message: { ...fields, type } }),
    openLink: (url: string) => send(SHELL_OPEN_LINK, { url }),
    disconnect: () => {
      boundWindow.removeEventListener("message", onMessage);
      boundWindow.removeEventListener("click", onLinkClick);
      boundWindow.removeEventListener("auxclick", onLinkClick);
      for (const type of ["pointerdown", "keydown"]) {
        boundWindow.removeEventListener(type, onInput, { capture: true });
      }
      if (boundWindow.open === routedOpen) boundWindow.open = nativeOpen;
      isDisconnected = true;
      settlePendingStartsAsNotAnswered();
    },
  };
}

/** A failure an agent is asked to fix: ``whatFailed`` is a sentence stem saying what failed, as ``failureSentence``
 *  takes ("This window couldn't be closed"), and ``details`` what went wrong underneath it (a status, a server's
 *  text, command output), handed on verbatim; null when there is none. */
export interface AgentFixRequest {
  readonly whatFailed: string;
  readonly details: string | null;
}

/** A Markdown fence for ``text`` that no run of backticks inside it can close. */
function fenceFor(text: string): string {
  const longestRun = Math.max(0, ...Array.from(text.matchAll(/`+/g), (run) => run[0].length));
  return "`".repeat(Math.max(3, longestRun + 1));
}

/** The one task every "Ask an agent to fix this" button gives the agent: what failed, the details as they are, and
 *  what to do about it. */
function agentFixTask({ whatFailed, details }: AgentFixRequest): string {
  const stem = whatFailed.trim();
  const parts = [/[.!?]$/.test(stem) ? stem : `${stem}.`];
  if (details !== null && details.trim() !== "") {
    const fence = fenceFor(details);
    parts.push(`Details:\n${fence}\n${details}\n${fence}`);
  }
  parts.push("Find out why this failed and fix it. Then tell me what was wrong and what you changed.");
  return parts.join("\n\n");
}

/** "Ask an agent to fix this": start a new chat whose first message asks an agent to fix ``failure``, through the
 *  shell's ``shell:start-new-with-text`` (a page passes its connection; the shell, which is not framed, its own
 *  ``startNewWithText``). A new chat, so the task does not land in the middle of a conversation about something
 *  else. Settles once the chat's window opened or the start failed. */
export function askAgentToFix(
  starter: Pick<ShellConnection, "startNewWithText">,
  failure: AgentFixRequest,
): Promise<StartNewOutcome> {
  return starter.startNewWithText(agentFixTask(failure));
}

/** A failure as a surface shows it: its sentence, what goes behind "See details" (null when nothing), and whether the
 *  sentence asks for a bug report. */
export interface ShownFailure {
  readonly message: string;
  readonly details: string | null;
  readonly isReportable: boolean;
}

/** How a ``startNewWithText`` went: the new window opened, or the failure to show where the press was. */
export type StartNewOutcome =
  { readonly isOpened: true } | { readonly isOpened: false; readonly failure: ShownFailure };

/** What a change nothing answered says, here since this module imports nothing (``models/failure.ts`` takes it from
 *  here). Never "nothing was changed": a gateway status can arrive after the request was carried out, and a
 *  connection that dropped after the request was sent looks the same as one that never opened. */
export const CHANGE_NOT_ANSWERED_SENTENCE =
  "The workspace didn't answer, so Imbue Studio can't tell whether this went through.";

const START_NOT_ANSWERED: StartNewOutcome = {
  isOpened: false,
  failure: { message: CHANGE_NOT_ANSWERED_SENTENCE, details: null, isReportable: false },
};

/** A request id no other page under the same chat root is likely to pick: the root hands every answer to all of its
 *  inner pages. */
function newRequestId(): string {
  return Array.from(crypto.getRandomValues(new Uint32Array(4)), (part) => part.toString(16)).join("");
}

/** The outcome a ``shell:start-new-with-text-result`` carries; one that says it failed without a readable failure
 *  shows the not-answered sentence's caution rather than nothing. */
function readStartNewOutcome(message: Readonly<Record<string, unknown>>): StartNewOutcome {
  if (message.isOpened === true) return { isOpened: true };
  const failure = message.failure;
  if (failure === null || typeof failure !== "object") return START_NOT_ANSWERED;
  const { message: sentence, details, isReportable } = failure as Record<string, unknown>;
  if (typeof sentence !== "string" || sentence.trim() === "") return START_NOT_ANSWERED;
  return {
    isOpened: false,
    failure: {
      message: sentence,
      details: typeof details === "string" ? details : null,
      isReportable: isReportable === true,
    },
  };
}

/** The failure a "Report a bug" is about: the sentence the user saw, and what was behind its "See details" (null when
 *  nothing was). */
export interface ReportedFailure {
  readonly message: string;
  readonly details: string | null;
}

/** The failure a page's ``shell:report-bug`` carries, or null when its payload holds none in that shape. */
export function reportedFailureOf(payload: Readonly<Record<string, unknown>>): ReportedFailure | null {
  const { message, details } = payload;
  if (typeof message !== "string" || message.trim() === "") return null;
  if (details !== null && details !== undefined && typeof details !== "string") return null;
  return { message, details: details ?? null };
}

/** The text a bug report carries for ``failure``: the sentence the user saw, then everything under it (the shape
 *  Imbue Studio's own reports carry). */
export function bugReportText({ message, details }: ReportedFailure): string {
  return details === null || details.trim() === "" ? message : `${message}\n\n${details}`;
}
