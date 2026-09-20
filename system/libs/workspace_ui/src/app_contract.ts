/**
 * The browser-side app contract (workspace app model, contracts.md section 10, extended for
 * the desktop interface by docs/system/blueprint/desktop-interface/contracts.md section 7):
 * the one postMessage module an app page imports to speak to the workspace shell that frames
 * it.
 *
 * Built as its own library entry (`vite.contract.config.ts`) and served by the shell at
 * `/_static/app_contract.js` with a permissive CORS header, so a page on any app origin can
 * import it; the chat document, which lives in this same source tree, imports the source
 * directly. It must therefore import nothing: the served file has no other dependencies.
 *
 * Trust: a page accepts a message only from `window.parent` (a nested third-party frame can
 * post here but can never satisfy that identity), and sends only to `window.parent`. The
 * target origin is `*` for the reason the minds embed contract gives: the workspace's own
 * `frame-ancestors` policy means only a workspace-family document can frame this page at all.
 * Unknown types are ignored and shipped types never change meaning; the contract evolves by
 * adding types.
 */

/** Shell to app: sent after every `load` of the frame; says which window, desktop, path, and client this page is in. */
export const SHELL_HANDSHAKE = "shell:handshake";
/** Shell to app: the tab became visible in this client. */
export const SHELL_SHOWN = "shell:shown";
/** Shell to app: the tab stopped being visible in this client. */
export const SHELL_HIDDEN = "shell:hidden";
/** Shell to app: the close chord fired while this tab was active. */
export const SHELL_CLOSE_REQUEST = "shell:close-request";
/** Shell to app: the window's path changed elsewhere; the page should show that path in place. */
export const SHELL_NAVIGATE = "shell:navigate";
/** App to shell: what this page can do, sent once on connect. */
export const SHELL_CAPABILITIES = "shell:capabilities";
/** App to shell: the page received focus; the shell activates its tab. */
export const SHELL_FOCUSED = "shell:focused";
/** App to shell: the page reports where it is and what it is called. */
export const SHELL_LOCATION = "shell:location";
/** App to shell: open another page of this app beside this one. */
export const SHELL_OPEN = "shell:open";

/**
 * What the shell says about the frame it created: the client, the window, its desktop, and the
 * path the window is at (desktop-interface contracts.md section 7). The client id is required;
 * a field the shell does not send, a page reads as "".
 */
export interface ShellHandshake {
  clientId: string;
  windowId: string;
  desktopId: string;
  path: string;
}

/**
 * What a page can do beyond the base contract. A page that handles `shell:navigate` in place
 * declares `navigation: true` and gives `onNavigate`; a shell then asks it to move rather than
 * reloading its frame.
 */
export interface ShellCapabilities {
  navigation: boolean;
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
  /** Stop listening to the shell. */
  disconnect(): void;
}

/** Raised when a page's handlers and its declared capabilities disagree. */
export class ShellContractError extends Error {}

const DEFAULT_CAPABILITIES: ShellCapabilities = { navigation: false };

function optionalString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function readHandshake(data: Record<string, unknown>): ShellHandshake | null {
  const { clientId, windowId, desktopId, path } = data;
  if (typeof clientId !== "string" || clientId === "") return null;
  return {
    clientId,
    windowId: optionalString(windowId),
    desktopId: optionalString(desktopId),
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
  return capabilities;
}

/**
 * Connect this page to the shell that frames it. Safe to call on a top-level page: nothing
 * arrives, and every send is a no-op, so an app behaves the same visited directly.
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

  boundWindow.addEventListener("message", onMessage);
  send(SHELL_CAPABILITIES, { navigation: capabilities.navigation });
  return {
    isFramed,
    focused: () => send(SHELL_FOCUSED, {}),
    location: (path: string, title: string) => send(SHELL_LOCATION, { path, title }),
    openPath: (path: string, ifPresent: OpenIfPresent) => send(SHELL_OPEN, { path, ifPresent }),
    disconnect: () => boundWindow.removeEventListener("message", onMessage),
  };
}
