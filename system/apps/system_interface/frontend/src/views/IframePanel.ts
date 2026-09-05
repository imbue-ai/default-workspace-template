import m from "mithril";
import { SHELL_HANDSHAKE, SHELL_HIDDEN, SHELL_SHOWN } from "../app_contract";
import { getClientId, getDeviceKind } from "../models/ClientIdentity";
import { sendToChildFrame } from "../relay";

/** What the shell tells a page that speaks the app contract (contracts.md section 10). */
export interface IframeContractAttrs {
  /** The tab's address. */
  address: string;
  /** The tab's id, which the shell mints per panel. */
  tabId: string;
  /** The view (a project id, or Everything) whose tab shows the page. */
  viewId: string;
  /** Whether the pane showing this frame is on screen right now. */
  isVisible: boolean;
}

/** The part of the handshake that can change while the page lives: a page outlives the pane
 *  and the view that showed it, so it is told again whenever these change. */
interface HandshakeIdentity {
  address: string;
  tabId: string;
  viewId: string;
}

function isSameIdentity(first: HandshakeIdentity, second: HandshakeIdentity): boolean {
  return first.address === second.address && first.tabId === second.tabId && first.viewId === second.viewId;
}

/** What a pane shows instead of the page while the app behind it is stopped. */
export interface StoppedAppPlaceholderAttrs {
  label: string;
  detail: string;
  /** Start the app; absent when the workspace cannot start it (unsupervised, or critical). */
  onStart: (() => void) | null;
}

interface IframePanelAttrs {
  url: string;
  title: string;
  /** The app the frame belongs to; every frame of one app reloads together on a Refresh. */
  appName: string;
  /** The address the frame shows, machine-wide: one page per instance, whichever pane
   *  (or project) shows it. */
  address: string;
  /** The shell hands the page a handshake on every load (and again when the tab or view
   *  showing it changes) and follows the pane's visibility with shown and hidden. */
  contract: IframeContractAttrs;
  /** Rendered in place of the frame while the app is stopped. */
  stopped?: StoppedAppPlaceholderAttrs | null;
  /** The sandbox flags, when the default set is not enough. */
  sandbox?: string;
}

export const IFRAME_PANEL_APP_ATTR = "data-app";
export const IFRAME_PANEL_ADDRESS_ATTR = "data-address";

// App pages are cross-origin iframes (each app owns its own origin), so allow-same-origin only
// lets the framed app be a normal page on ITS origin -- it grants nothing on the shell's.
export const DEFAULT_FRAME_SANDBOX = "allow-scripts allow-same-origin allow-forms allow-popups";

// An app page may open transcript links in real tabs, download files, and raise modals, none
// of which a sandboxed frame may do without these. Every instance frame gets them: the shell
// does not know which apps need which.
export const APP_FRAME_SANDBOX = `${DEFAULT_FRAME_SANDBOX} allow-popups-to-escape-sandbox allow-downloads allow-modals`;

export function IframePanel(): m.Component<IframePanelAttrs> {
  let frame: HTMLIFrameElement | null = null;
  let latestContract: IframeContractAttrs | null = null;
  // What the page was last told, so a handshake goes out again only when its identity changed
  // and shown or hidden only when the visibility did; a load resets both, since a fresh page
  // has been told nothing.
  let lastSentIdentity: HandshakeIdentity | null = null;
  let lastSentVisibility: boolean | null = null;

  function syncVisibility(): void {
    // Nothing until the page has had its handshake: before its load there is nobody listening.
    if (frame === null || latestContract === null || lastSentIdentity === null) return;
    if (lastSentVisibility === latestContract.isVisible) return;
    lastSentVisibility = latestContract.isVisible;
    sendToChildFrame(frame, latestContract.isVisible ? SHELL_SHOWN : SHELL_HIDDEN);
  }

  function greet(): void {
    if (frame === null || latestContract === null) return;
    const identity: HandshakeIdentity = {
      address: latestContract.address,
      tabId: latestContract.tabId,
      viewId: latestContract.viewId,
    };
    sendToChildFrame(frame, SHELL_HANDSHAKE, {
      clientId: getClientId(),
      deviceKind: getDeviceKind(),
      ...identity,
    });
    lastSentIdentity = identity;
  }

  function greetOnLoad(): void {
    lastSentIdentity = null;
    lastSentVisibility = null;
    greet();
    syncVisibility();
  }

  /** A page that has had its handshake is told again when the tab or view showing it changed.
   *  A page no tab shows keeps the identity it was last told: there is no tab to name, and it
   *  is told again when a tab shows it. */
  function syncIdentity(): void {
    if (lastSentIdentity === null || latestContract === null || latestContract.tabId === "") return;
    if (isSameIdentity(lastSentIdentity, latestContract)) return;
    greet();
  }

  return {
    view(vnode) {
      const { url, title, appName, address, contract, stopped, sandbox } = vnode.attrs;
      latestContract = contract;
      // A stopped app's pane shows a lightweight placeholder instead of the dead iframe's raw
      // connection error. Rendered per redraw off the inventory, so a start (from anywhere)
      // swaps the iframe back in on the next ``apps_updated`` push.
      if (stopped != null) {
        return m(StoppedAppPlaceholder, { ...stopped, appName });
      }
      return m("iframe", {
        src: url,
        title,
        style: "width: 100%; height: 100%; border: none;",
        sandbox: sandbox ?? APP_FRAME_SANDBOX,
        // Let embedded apps (the browser fleet viewer) reach the user's clipboard.
        allow: "clipboard-read; clipboard-write",
        [IFRAME_PANEL_APP_ATTR]: appName,
        [IFRAME_PANEL_ADDRESS_ATTR]: address,
        oncreate: (created: m.VnodeDOM) => {
          frame = created.dom as HTMLIFrameElement;
          // Every load, not just the first: a reload (the tab's Refresh, or the page's own) is
          // a fresh page that has to be told who it is again.
          frame.addEventListener("load", greetOnLoad);
        },
        onupdate: () => {
          syncIdentity();
          syncVisibility();
        },
        onremove: () => {
          frame = null;
          lastSentIdentity = null;
        },
      });
    },
  };
}

/**
 * The minimal stopped-tab state: what the pane is, that it is stopped, and nothing else --
 * deliberately no log tail and no diagnostics, since Stop is a reversible, ordinary state
 * rather than an error. The Start button appears only where the app is startable through the
 * workspace; the ``apps_updated`` push after a start swaps the iframe back in.
 */
const StoppedAppPlaceholder: m.Component<StoppedAppPlaceholderAttrs & { appName: string }> = {
  view(vnode) {
    const { label, detail, onStart, appName } = vnode.attrs;
    return m(
      "div",
      {
        class: "si-stopped-app flex h-full w-full flex-col items-center justify-center gap-3 bg-surface",
        [IFRAME_PANEL_APP_ATTR]: appName,
      },
      [
        m("div", { class: "text-[15px] font-medium text-text-primary" }, label),
        m("div", { class: "text-[13px] text-text-faint" }, detail),
        onStart === null
          ? null
          : m(
              "button",
              {
                type: "button",
                class:
                  "si-stopped-app-start mt-1 flex h-8 cursor-pointer items-center rounded-md border " +
                  "border-border px-4 text-[13px] font-medium text-text-primary hover:bg-bg-hover",
                onclick: onStart,
              },
              `Start ${label}`,
            ),
      ],
    );
  },
};

function reloadFrame(iframe: HTMLIFrameElement): void {
  try {
    const win = iframe.contentWindow;
    if (win !== null) {
      win.location.reload();
      return;
    }
  } catch {
    // Cross-origin iframe: fall through to src reassignment.
  }
  const currentSrc = iframe.getAttribute("src");
  if (currentSrc !== null) {
    iframe.setAttribute("src", currentSrc);
  }
}

/** Reload every frame of one app, backgrounded ones included: a page stays live whether or
 *  not a view is showing it, and "refresh the app" has always meant the app rather than one
 *  pane. Cross-origin frames reload by ``src`` reassignment. Answers how many were reloaded. */
export function reloadIframesForApp(appName: string): number {
  const iframes = document.querySelectorAll<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_APP_ATTR}="${CSS.escape(appName)}"]`,
  );
  iframes.forEach(reloadFrame);
  return iframes.length;
}

/** Reload the one frame showing ``address``. Answers whether there was one. */
export function reloadIframeForAddress(address: string): boolean {
  const iframe = document.querySelector<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_ADDRESS_ATTR}="${CSS.escape(address)}"]`,
  );
  if (iframe === null) return false;
  reloadFrame(iframe);
  return true;
}
