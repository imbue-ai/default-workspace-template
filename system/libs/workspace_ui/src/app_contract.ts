/**
 * The browser-side app contract (workspace app model, contracts.md section 10): the one
 * postMessage module an app page imports to speak to the workspace shell that frames it.
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

/** Shell to app: sent after every `load` of the frame; says which tab and client this page is in. */
export const SHELL_HANDSHAKE = "shell:handshake";
/** Shell to app: the tab became visible in this client. */
export const SHELL_SHOWN = "shell:shown";
/** Shell to app: the tab stopped being visible in this client. */
export const SHELL_HIDDEN = "shell:hidden";
/** Shell to app: the close chord fired while this tab was active. */
export const SHELL_CLOSE_REQUEST = "shell:close-request";
/** App to shell: the page received focus; the shell activates its tab. */
export const SHELL_FOCUSED = "shell:focused";
/** App to shell: the page reports where it is; the shell relays it to the owning app. */
export const SHELL_LOCATION = "shell:location";
/** App to shell: dock an instance of this app beside this tab. */
export const SHELL_OPEN = "shell:open";
/** App to shell: the new-tab chord fired while this page had focus; open a New Tab. */
export const SHELL_NEW_TAB = "shell:new-tab";

/** The keydown fields the new-tab chord is decided from. */
export interface ChordKeys {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
}

/**
 * Whether this browser reports an Apple platform, whose accelerator key is Cmd rather than Ctrl.
 *
 * Prefers `userAgentData` over the deprecated `navigator.platform`, with the UA string as the
 * fallback -- the shape `models/ClientIdentity` uses for the device kind.
 */
export function isApplePlatform(): boolean {
  const uaData = (navigator as { userAgentData?: { platform?: string } }).userAgentData;
  return /mac|iphone|ipad|ipod/i.test(uaData?.platform ?? navigator.userAgent);
}

/**
 * Whether a keydown is the workspace's new-tab chord: Cmd+T on Apple platforms, Ctrl+T elsewhere.
 *
 * Only the desktop client delivers it to a page at all; every browser keeps Cmd/Ctrl+T for a
 * browser tab of its own.
 */
export function isNewTabChord(keys: ChordKeys, isApple: boolean): boolean {
  if (keys.key !== "t" && keys.key !== "T") return false;
  if (keys.altKey || keys.shiftKey) return false;
  return isApple ? keys.metaKey && !keys.ctrlKey : keys.ctrlKey && !keys.metaKey;
}

export interface ShellHandshake {
  clientId: string;
  deviceKind: string;
  viewId: string;
  address: string;
  tabId: string;
}

export interface ShellConnectionHandlers {
  onHandshake?: (handshake: ShellHandshake) => void;
  onShown?: () => void;
  onHidden?: () => void;
  onCloseRequest?: () => void;
}

export interface ShellConnection {
  /** Whether a shell frames this page at all; a top-level visit has no shell to talk to. */
  readonly isFramed: boolean;
  /** Tell the shell this page received focus. */
  focused(): void;
  /** Report where this page is now (a path under the app's origin). */
  location(path: string): void;
  /** Ask the shell to dock an instance of this app beside this tab. */
  open(address: string): void;
  /** Stop listening to the shell. */
  disconnect(): void;
}

function readHandshake(data: Record<string, unknown>): ShellHandshake | null {
  const { clientId, deviceKind, viewId, address, tabId } = data;
  if (typeof clientId !== "string" || typeof deviceKind !== "string") return null;
  if (typeof viewId !== "string" || typeof address !== "string" || typeof tabId !== "string") return null;
  return { clientId, deviceKind, viewId, address, tabId };
}

/**
 * Connect this page to the shell that frames it. Safe to call on a top-level page: nothing
 * arrives, and every send is a no-op, so an app behaves the same visited directly.
 */
export function connectToShell(handlers: ShellConnectionHandlers): ShellConnection {
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
      default:
        return;
    }
  }

  function send(type: string, payload: Record<string, unknown>): void {
    if (!isFramed) return;
    boundWindow.parent.postMessage({ type, ...payload }, "*");
  }

  // Every tab is a frame, so a keydown here never reaches the shell's own document; the chord is
  // carried up from wherever focus actually is. Only while framed: a page visited directly has no
  // shell to open a tab in, and would swallow the browser's own chord for nothing.
  function onKeyDown(event: KeyboardEvent): void {
    if (!isFramed || !isNewTabChord(event, isApplePlatform())) return;
    event.preventDefault();
    send(SHELL_NEW_TAB, {});
  }

  boundWindow.addEventListener("message", onMessage);
  boundWindow.addEventListener("keydown", onKeyDown);
  return {
    isFramed,
    focused: () => send(SHELL_FOCUSED, {}),
    location: (path: string) => send(SHELL_LOCATION, { path }),
    open: (address: string) => send(SHELL_OPEN, { address }),
    disconnect: () => {
      boundWindow.removeEventListener("message", onMessage);
      boundWindow.removeEventListener("keydown", onKeyDown);
    },
  };
}
