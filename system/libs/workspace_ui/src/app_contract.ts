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
 *
 * A widget's page is dragged by a press anywhere on it (``widgetDragTracker``): once a primary press travels
 * ``WIDGET_DRAG_THRESHOLD_PX``, the page reports the drag to the shell as ``shell:widget-drag`` (start, every move,
 * then end or cancel), in screen coordinates, which stay true while the shell moves the frame under the pointer;
 * the click that would end it is swallowed. A press that never travels that far is the page's click, and a press
 * on a control (a button, a field, an element marked ``data-no-drag``) is never a drag.
 *
 * A widget's page gives an element a tooltip with ``data-widget-tooltip`` (``widgetTooltipTracker``): the page's own
 * bubble would be clipped by its small frame, so the page says which element the pointer entered, with its text and
 * where it is in the frame, as ``shell:widget-tooltip``, and the shell shows the workspace's tooltip there; leaving
 * every such element, or pressing, sends the same type with no text.
 *
 * A right-click on a widget's page asks the shell for the widget's menu (``shell:widget-menu``) rather than opening
 * the browser's. In the taskbar (mode ``tray``, or ``toolbar`` in the taskbar's row) a widget does not move, and a
 * click on it is not the page's: it asks the shell to open or close the widget's popover (``shell:widget-press``), and
 * Escape on it asks the shell to close the popover (``shell:widget-dismiss``), since the page then holds the keyboard;
 * a page in the taskbar needs no code for any.
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
/** App to shell: open a link that is not the page's own -- a local URL, one of the workspace's app addresses (another
 *  app's page) or another workspace's, a ``file:`` URL, an external web, ``mailto:``, ``tel:`` or custom-scheme link
 *  -- which only the shell can put where it belongs: it opens the app's window there (for a local URL, the app on whose
 *  ``<label>.localhost`` host or registered port it is, else the workspace's browser), the file in Files, an
 *  external link in the app registered for it or else outside the workspace, or says why it cannot. */
export const SHELL_OPEN_LINK = "shell:open-link";
/** Shell to app, to a widget only: how the client draws it (``WidgetMode``), after every handshake and on a change. */
export const SHELL_WIDGET_MODE = "shell:widget-mode";
/** App to shell, from a widget only: the CSS pixel size of its content in its current mode. */
export const SHELL_WIDGET_SIZE = "shell:widget-size";
/** App to shell, from a widget only: whether it has nothing to show, so the shell draws nothing for it. */
export const SHELL_WIDGET_HIDDEN = "shell:widget-hidden";
/** App to shell, from a widget only: a press on the page is dragging the widget (see the module docs). */
export const SHELL_WIDGET_DRAG = "shell:widget-drag";

/** App to shell, from a widget only: the tooltip of the element the pointer is over, or none (see the module docs). */
export const SHELL_WIDGET_TOOLTIP = "shell:widget-tooltip";
/** App to shell, from a widget in the taskbar only: a click on the page, which opens or closes its popover. */
export const SHELL_WIDGET_PRESS = "shell:widget-press";
/** App to shell, from a widget only: a right-click on the page at ``x``, ``y`` (the frame's pixels), which opens the
 *  shell's menu for the widget. */
export const SHELL_WIDGET_MENU = "shell:widget-menu";
/** App to shell, from a widget only: Escape was pressed on the page, which closes the widget's popover if it is
 *  open. */
export const SHELL_WIDGET_DISMISS = "shell:widget-dismiss";
/** App to shell, from any page of an app that declares a widget: put the app's widget on the desktop, in the taskbar's
 *  tray or in its row, for this client, as its menu's Move items do (app-widgets plan section 4.10). */
export const SHELL_WIDGET_PLACEMENT = "shell:widget-placement";
/** The attribute a widget page's element carries its tooltip's text in. */
export const WIDGET_TOOLTIP_ATTRIBUTE = "data-widget-tooltip";

/** How far a press on a widget's page travels before it is a drag rather than a click, in CSS pixels. */
export const WIDGET_DRAG_THRESHOLD_PX = 4;

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

/** What frames the page: a window, or a widget on the desktop (app-widgets plan section 6). */
export type ShellSurface = "window" | "widget";

/** How the client draws a widget: in full, as its one-line pill, in the taskbar's tray, or in the taskbar's row
 *  among the windows' entries (each only for a page the manifest says draws that form; app-widgets plan section
 *  4.10). */
export type WidgetMode = "expanded" | "collapsed" | "tray" | "toolbar";

/** Where a client draws an app's widget: on the desktop, in the taskbar's tray, or in the taskbar's row. */
export type WidgetPlacement = "desktop" | "tray" | "toolbar";

/**
 * What the shell says about the frame it created: the client, the window, its desktop, the app
 * the window belongs to, and the path the window is at (desktop-interface contracts.md section
 * 7). The client id is required; a field the shell does not send, a page reads as "".
 * ``surface`` says whether that frame is a window or a widget.
 */
export interface ShellHandshake {
  clientId: string;
  /** The window's id; empty for a widget, which has none. */
  windowId: string;
  desktopId: string;
  app: string;
  path: string;
  surface: ShellSurface;
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
  /** A widget's mode, after every handshake and whenever the client changes it; never sent to a window. */
  onWidgetMode?: (mode: WidgetMode) => void;
  capabilities?: ShellCapabilities;
}

export interface ShellConnection {
  /** Whether anything frames this page; a top-level visit has no shell to talk to. */
  readonly isFramed: boolean;
  /** Whether a shell frames this page: its handshake has arrived. */
  hasShell(): boolean;
  /** What frames this page, as the last handshake said; a window until one arrives. */
  surface(): ShellSurface;
  /** Tell the shell this page received focus; nothing from a widget. */
  focused(): void;
  /** Report where this page is now (a path under the app's origin) and what it is called; nothing from a widget. */
  location(path: string, title: string): void;
  /** Ask the shell to open a page of this app at a path beside this one. */
  openPath(path: string, ifPresent: OpenIfPresent): void;
  /** Ask the shell to start something with ``text``: its launcher's primary text action (a new chat on a stock machine). */
  startWithText(text: string): void;
  /** Ask the shell to draft ``text`` into a chat's composer, unsent (the chat on screen on a stock machine). */
  draftText(text: string): void;
  /** Send the shell a message for the apps registered for ``type``, with ``fields`` as its own fields. */
  sendMessage(type: string, fields: Readonly<Record<string, unknown>>): void;
  /** Ask the shell to open a local URL (as the window of the app on its ``<label>.localhost`` host or at its port, else
   *  in the workspace's browser), an address of the workspace's apps (or another workspace's) as a window, a ``file:``
   *  URL in Files, or an external web, ``mailto:``, ``tel:`` or custom-scheme link in the app registered for it, else
   *  outside the workspace. */
  openLink(url: string): void;
  /** Tell the shell how big a widget's content is in its current mode, in CSS pixels; nothing from a window. */
  widgetSize(width: number, height: number): void;
  /** Tell the shell a widget has nothing to show (true) or has again (false); nothing from a window. */
  widgetHidden(isHidden: boolean): void;
  /** Ask the shell to put this app's widget on the desktop, in the taskbar's tray or in its row, for this client; the
   *  shell does so only for an app that declares a widget (and, in the taskbar, one whose page draws that form). */
  placeWidget(placement: WidgetPlacement): void;
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
  const { clientId, windowId, desktopId, app, path, surface } = data;
  if (typeof clientId !== "string" || clientId === "") return null;
  return {
    clientId,
    windowId: optionalString(windowId),
    desktopId: optionalString(desktopId),
    app: optionalString(app),
    path: optionalString(path),
    surface: surface === "widget" ? "widget" : "window",
  };
}

function readWidgetMode(value: unknown): WidgetMode | null {
  return value === "expanded" || value === "collapsed" || value === "tray" || value === "toolbar" ? value : null;
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

/** The browser's own schemes a framed page hands on when a link to them is clicked, as it does every custom-scheme
 *  link; any other (``javascript:``, ``blob:``, ``data:``) is left to the page. */
const HANDED_ON_SCHEMES: ReadonlySet<string> = new Set(["http:", "https:", "file:", "mailto:", "tel:"]);

function isHandedOn(url: URL): boolean {
  return HANDED_ON_SCHEMES.has(url.protocol) || isCustomSchemeUrl(url);
}
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

/** The elements a press on which is never a drag: the page's controls, and anything it marks ``data-no-drag``. */
const UNDRAGGABLE_SELECTOR = "button, input, textarea, select, [contenteditable], [data-no-drag]";

/** Where a widget drag's press began: in the frame (the page's client pixels) and on the screen. */
interface WidgetPress {
  readonly pointerId: number;
  readonly clientX: number;
  readonly clientY: number;
  readonly screenX: number;
  readonly screenY: number;
  isDragging: boolean;
}

/**
 * Turn presses on a widget's page into drags reported to the shell (see the module docs); ``isWidget`` says whether
 * a shell frames the page as a widget now. Returns the listeners to add to the page's window, each named by its
 * event.
 */
function widgetDragTracker(
  isWidget: () => boolean,
  send: (type: string, payload: Record<string, unknown>) => void,
): Readonly<Record<string, (event: Event) => void>> {
  let press: WidgetPress | null = null;
  let isClickSwallowed = false;
  const end = (event: PointerEvent, phase: "end" | "cancel"): void => {
    if (press === null || event.pointerId !== press.pointerId) return;
    if (press.isDragging) {
      send(SHELL_WIDGET_DRAG, phase === "end" ? { phase, screenX: event.screenX, screenY: event.screenY } : { phase });
      isClickSwallowed = phase === "end";
    }
    press = null;
  };
  return {
    pointerdown: (event) => {
      const pointer = event as PointerEvent;
      isClickSwallowed = false;
      if (!isWidget() || pointer.button !== PRIMARY_BUTTON || !pointer.isPrimary) return;
      const target = pointer.target;
      if (target instanceof Element && target.closest(UNDRAGGABLE_SELECTOR) !== null) return;
      press = {
        pointerId: pointer.pointerId,
        clientX: pointer.clientX,
        clientY: pointer.clientY,
        screenX: pointer.screenX,
        screenY: pointer.screenY,
        isDragging: false,
      };
    },
    pointermove: (event) => {
      const pointer = event as PointerEvent;
      if (press === null || pointer.pointerId !== press.pointerId) return;
      if (!press.isDragging) {
        const travel = Math.hypot(pointer.screenX - press.screenX, pointer.screenY - press.screenY);
        if (travel < WIDGET_DRAG_THRESHOLD_PX) return;
        press.isDragging = true;
        // Keep the moves coming once the pointer outruns the frame, as it does at the backdrop's edge.
        if (pointer.target instanceof Element) pointer.target.setPointerCapture?.(pointer.pointerId);
        send(SHELL_WIDGET_DRAG, {
          phase: "start",
          pressX: press.clientX,
          pressY: press.clientY,
          pressScreenX: press.screenX,
          pressScreenY: press.screenY,
          screenX: pointer.screenX,
          screenY: pointer.screenY,
        });
        return;
      }
      send(SHELL_WIDGET_DRAG, { phase: "move", screenX: pointer.screenX, screenY: pointer.screenY });
    },
    pointerup: (event) => end(event as PointerEvent, "end"),
    pointercancel: (event) => end(event as PointerEvent, "cancel"),
    // A press on a link would otherwise start the browser's own drag of it, which ends the pointer's events.
    dragstart: (event) => {
      if (isWidget()) event.preventDefault();
    },
    click: (event) => {
      if (!isClickSwallowed) return;
      isClickSwallowed = false;
      event.preventDefault();
      event.stopImmediatePropagation();
    },
  };
}

/**
 * Report the tooltip of the element under the pointer on a widget's page to the shell (see the module docs);
 * ``isWidget`` says whether a shell frames the page as a widget now. Returns the listeners to add to the page's
 * window, each named by its event.
 */
function widgetTooltipTracker(
  isWidget: () => boolean,
  send: (type: string, payload: Record<string, unknown>) => void,
): Readonly<Record<string, (event: Event) => void>> {
  let shown: Element | null = null;
  const hide = (): void => {
    if (shown === null) return;
    shown = null;
    send(SHELL_WIDGET_TOOLTIP, { text: null });
  };
  return {
    mouseover: (event) => {
      if (!isWidget()) return;
      const trigger = event.target instanceof Element ? event.target.closest(`[${WIDGET_TOOLTIP_ATTRIBUTE}]`) : null;
      const text = trigger?.getAttribute(WIDGET_TOOLTIP_ATTRIBUTE) ?? null;
      if (trigger === null || text === null || text === "") {
        hide();
        return;
      }
      if (trigger === shown) return;
      shown = trigger;
      const box = trigger.getBoundingClientRect();
      send(SHELL_WIDGET_TOOLTIP, { text, left: box.left, top: box.top, width: box.width, height: box.height });
    },
    mouseout: (event) => {
      if ((event as MouseEvent).relatedTarget === null) hide();
    },
    pointerdown: () => hide(),
  };
}

/**
 * Hand a widget page's right-click, and in the taskbar its click, to the shell (see the module docs). Returns the
 * listeners to add to the page's window, each named by its event.
 */
function widgetPressTracker(
  isWidget: () => boolean,
  isInTaskbar: () => boolean,
  send: (type: string, payload: Record<string, unknown>) => void,
): Readonly<Record<string, (event: Event) => void>> {
  return {
    contextmenu: (event) => {
      if (!isWidget()) return;
      event.preventDefault();
      const mouse = event as MouseEvent;
      send(SHELL_WIDGET_MENU, { x: mouse.clientX, y: mouse.clientY });
    },
    keydown: (event) => {
      if (isWidget() && (event as KeyboardEvent).key === "Escape") send(SHELL_WIDGET_DISMISS, {});
    },
    click: (event) => {
      if (!isInTaskbar() || (event as MouseEvent).button !== PRIMARY_BUTTON) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      send(SHELL_WIDGET_PRESS, {});
    },
  };
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
  let surface: ShellSurface = "window";
  let widgetMode: WidgetMode | null = null;

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
        surface = handshake.surface;
        handlers.onHandshake?.(handshake);
        return;
      }
      case SHELL_WIDGET_MODE: {
        const mode = readWidgetMode(message.mode);
        if (mode === null || surface !== "widget") return;
        widgetMode = mode;
        handlers.onWidgetMode?.(mode);
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

  const isWidget = (): boolean => hasShell && surface === "widget";
  const isInTaskbar = (): boolean => isWidget() && (widgetMode === "tray" || widgetMode === "toolbar");
  const dragListeners = widgetDragTracker(() => isWidget() && !isInTaskbar(), send);
  const tooltipListeners = widgetTooltipTracker(isWidget, send);
  const pressListeners = widgetPressTracker(isWidget, isInTaskbar, send);

  boundWindow.addEventListener("message", onMessage);
  if (isFramed) {
    boundWindow.addEventListener("click", onLinkClick);
    boundWindow.addEventListener("auxclick", onLinkClick);
    // In the capture phase, so a swallowed click never reaches the page's handlers or the link rule.
    for (const [type, listener] of Object.entries(dragListeners)) boundWindow.addEventListener(type, listener, true);
    for (const listeners of [tooltipListeners, pressListeners]) {
      for (const [type, listener] of Object.entries(listeners)) boundWindow.addEventListener(type, listener, true);
    }
    boundWindow.open = routedOpen;
  }
  send(SHELL_CAPABILITIES, { navigation: capabilities.navigation, closeChord: capabilities.closeChord });
  return {
    isFramed,
    hasShell: () => hasShell,
    surface: () => surface,
    focused: () => {
      if (surface === "window") send(SHELL_FOCUSED, {});
    },
    location: (path: string, title: string) => {
      if (surface === "window") send(SHELL_LOCATION, { path, title });
    },
    openPath: (path: string, ifPresent: OpenIfPresent) => send(SHELL_OPEN, { path, ifPresent }),
    startWithText: (text: string) => send(SHELL_START_WITH_TEXT, { text }),
    draftText: (text: string) => send(SHELL_DRAFT_TEXT, { text }),
    sendMessage: (type: string, fields: Readonly<Record<string, unknown>>) =>
      send(SHELL_MESSAGE, { message: { ...fields, type } }),
    openLink: (url: string) => send(SHELL_OPEN_LINK, { url }),
    widgetSize: (width: number, height: number) => {
      if (surface === "widget") send(SHELL_WIDGET_SIZE, { width, height });
    },
    widgetHidden: (isHidden: boolean) => {
      if (surface === "widget") send(SHELL_WIDGET_HIDDEN, { isHidden });
    },
    placeWidget: (placement: WidgetPlacement) => send(SHELL_WIDGET_PLACEMENT, { placement }),
    disconnect: () => {
      boundWindow.removeEventListener("message", onMessage);
      boundWindow.removeEventListener("click", onLinkClick);
      boundWindow.removeEventListener("auxclick", onLinkClick);
      for (const listeners of [dragListeners, tooltipListeners, pressListeners]) {
        for (const [type, listener] of Object.entries(listeners))
          boundWindow.removeEventListener(type, listener, true);
      }
      if (boundWindow.open === routedOpen) boundWindow.open = nativeOpen;
    },
  };
}
