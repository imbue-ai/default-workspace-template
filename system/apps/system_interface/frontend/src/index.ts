import m from "mithril";
import { getClientId } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import {
  DETACHED_WINDOWS,
  EMBEDDER_CAPABILITIES,
  OPEN_EXTERNAL,
  OPEN_LINK,
  POP_OUT_WINDOW,
  REATTACH_WINDOW,
  TEAR_OUT,
  WINDOW_DRAG_ENDED,
  WINDOW_DRAG_STARTED,
  announceReadyToEmbedder,
  sendToEmbedder,
  setEmbedderMessageHandler,
  setEmbedderMessageObserver,
} from "@imbue/workspace-ui/src/embed";
import "./style.css";
import { installCursorHidingWhileTyping } from "@imbue/workspace-ui/src/hideCursorWhileTyping";
import * as api from "./model/api";
import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./model/deepLinks";
import type { DeepLink } from "./model/deepLinks";
import { parseSoloMode } from "./model/soloMode";
import { isPreviewShell } from "./model/PreviewShell";
import type { Frame } from "./model/records";
import { frameFromViewportFractions } from "./geometry/frames";
import type { OutsideLinkOpener, PopOutBridge } from "./store/DesktopStore";
import { PointerGestureSource } from "./gestures/pointerGestures";
import { startPresenceHeartbeat } from "./model/Presence";
import { initEmbedderRelay } from "./relay";
import { reloadInterface } from "./reload";
import { DesktopStore } from "./store/DesktopStore";
import { ShellSocket } from "./store/socket";
import { followRenderModes } from "./theme/metrics";
import { App, BACKDROP_AREA_ATTRIBUTE } from "./views/App";

/** Rewrite the page's URL with its query string put through ``strip``, leaving the path, the hash, and the
 *  history entry as they are: how a boot-time parameter is removed once it has been read. */
function stripLocationSearch(strip: (search: string) => string): void {
  const stripped = `${window.location.pathname}${strip(window.location.search)}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", stripped);
}

/** The deep link the page was opened with (contracts.md section 9), removed from the URL as it is read. */
function takeDeepLinkFromLocation(): DeepLink {
  const link = parseDeepLink(window.location.search);
  if (!isDeepLinkEmpty(link)) stripLocationSearch(stripDeepLinkParams);
  return link;
}

/** The frame a reattach message names, when it names one: four finite fractions of this page's viewport, which is
 *  how the chrome measures a drop back onto the desktop, mapped onto the backdrop the desktop's frames are
 *  fractions of (and clamped by the verb). */
function frameFromMessage(value: unknown): Frame | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const numbers = ["x", "y", "width", "height"].map((key) => record[key]);
  if (!numbers.every((number) => typeof number === "number" && Number.isFinite(number))) return null;
  const [x, y, width, height] = numbers as number[];
  const frame = { x, y, width, height };
  const backdrop = document.querySelector<HTMLElement>(`[${BACKDROP_AREA_ATTRIBUTE}]`);
  if (backdrop === null) return frame;
  const box = backdrop.getBoundingClientRect();
  return frameFromViewportFractions(
    frame,
    { width: window.innerWidth, height: window.innerHeight },
    { x: box.x, y: box.y, width: box.width, height: box.height },
  );
}

/** The shell's side of the pull-out conversation: every ask goes to the embedding chrome. */
const popOutBridge: PopOutBridge = {
  requestPopOut: (request) => sendToEmbedder(POP_OUT_WINDOW, { ...request }),
  beginWindowDrag: (request) => sendToEmbedder(WINDOW_DRAG_STARTED, { ...request }),
  endWindowDrag: (windowId, isDetached, isCancelled) =>
    sendToEmbedder(WINDOW_DRAG_ENDED, { windowId, isDetached, isCancelled }),
  reportDetachedWindows: (windows) => sendToEmbedder(DETACHED_WINDOWS, { windows: [...windows] }),
};

/** Where an external link no app takes opens: through the chrome, or in a new tab of the browser this shell is in. */
const outsideLinks: OutsideLinkOpener = {
  openInEmbedder: (url) => sendToEmbedder(OPEN_EXTERNAL, { url }),
  openInBrowser: (url) => void window.open(url, "_blank", "noopener,noreferrer"),
};

function bootstrap(): void {
  const clientId = getClientId();
  // Read and left in the URL, unlike the deep link: a reload of a pulled-out window's page must come back as it.
  const solo = parseSoloMode(window.location.search);
  const root = document.documentElement;
  const readStyle = (element: HTMLElement): CSSStyleDeclaration => getComputedStyle(element);
  let store: DesktopStore | null = null;
  followRenderModes(
    root,
    (query) => window.matchMedia(query),
    readStyle,
    (modes, metrics) => {
      if (store === null) {
        store = new DesktopStore({
          clientId,
          api,
          socket: new ShellSocket(clientId),
          metrics,
          modes,
          redraw: () => m.redraw(),
          reloadInterface,
          popOut: popOutBridge,
          outsideLinks,
          soloWindowId: solo?.windowId ?? null,
          isSoloReopened: solo?.isReopened ?? false,
        });
      } else {
        store.setThemeMetrics(metrics, modes);
      }
    },
  );
  if (store === null) throw new Error("the render modes never reported");
  const desktopStore: DesktopStore = store;
  const gestures = new PointerGestureSource();
  // Say this window is here (and learn who it is) for as long as it stays visible.
  startPresenceHeartbeat();
  // The pointer hides while text is typed into the shell's own fields (the launcher, the settings);
  // each framed page does the same for itself.
  installCursorHidingWhileTyping(document);
  // The child-frame boundary: the minds relay for the framed pages' `minds:` messages, and the
  // shell side of the app contract.
  initEmbedderRelay();
  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, () => void desktopStore.closeFocusedWindow());
  // A phone's page sleeps while it is out of sight; coming back, the store reads the shell's word again.
  document.addEventListener("visibilitychange", () =>
    desktopStore.onVisibilityChange(document.visibilityState === "visible"),
  );
  // Every message the chrome sends also goes to the apps registered for its type.
  setEmbedderMessageObserver((message) => void desktopStore.relayEmbedderMessage(message, null));
  // The pull-out conversation's two asks from the chrome: what it can do, and a window to bring back.
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    desktopStore.setCanPopOut(message.canPopOut === true);
    desktopStore.setCanOpenLinksOutside(message.opensExternalLinks === true);
  });
  setEmbedderMessageHandler(REATTACH_WINDOW, (message) => {
    const windowId = message.windowId;
    if (typeof windowId !== "string" || windowId === "") return;
    void desktopStore.reattachWindow(windowId, frameFromMessage(message.frame));
  });
  // A popup a page of this workspace opened, which Imbue Studio turned away from a window of its own: it opens here.
  // A preview shell sees it only as the live shell's rebroadcast, and the live shell opens it.
  setEmbedderMessageHandler(OPEN_LINK, (message) => {
    if (isPreviewShell()) return;
    const url = message.url;
    if (typeof url !== "string" || url === "") return;
    void desktopStore.openLink(url, window.location.host, null);
  });
  setEmbedderMessageHandler(TEAR_OUT, (message) => {
    const windowId = message.windowId;
    const phase = message.phase;
    if (typeof windowId !== "string" || windowId === "") return;
    if (phase !== "out" && phase !== "in" && phase !== "released") return;
    desktopStore.setTearOut(windowId, phase);
  });
  const rootElement = document.getElementById("app");
  if (rootElement) {
    m.mount(rootElement, {
      view: () =>
        m(App, {
          store: desktopStore,
          gestures,
          host: window.location.host,
          protocol: window.location.protocol,
        }),
    });
  }
  const started = desktopStore.start(takeDeepLinkFromLocation());
  // Announced once the page can act on what the embedder held, not merely once a handler is
  // registered: relaying a message needs the apps (which of them take it), and the embedder sends
  // what it held the moment this lands.
  void Promise.all([started, desktopStore.whenAppsLoaded()]).then(() => announceReadyToEmbedder({ opensLinks: true }));
}

window.addEventListener("load", bootstrap);
