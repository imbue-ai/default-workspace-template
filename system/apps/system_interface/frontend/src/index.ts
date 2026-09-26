import m from "mithril";
import { getClientId } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import {
  DETACHED_WINDOWS,
  EMBEDDER_CAPABILITIES,
  FOCUS_CHAT,
  POP_OUT_CANCEL,
  POP_OUT_END,
  POP_OUT_WINDOW,
  REATTACH_WINDOW,
  announceReadyToEmbedder,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";
import "./style.css";
import * as api from "./model/api";
import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./model/deepLinks";
import type { DeepLink } from "./model/deepLinks";
import { parseSoloWindowId, stripSoloParam } from "./model/soloMode";
import type { Frame } from "./model/records";
import type { PopOutBridge } from "./store/DesktopStore";
import { PointerGestureSource } from "./gestures/pointerGestures";
import { startPresenceHeartbeat } from "./model/Presence";
import { initEmbedderRelay } from "./relay";
import { reloadInterface } from "./reload";
import { DesktopStore } from "./store/DesktopStore";
import { ShellSocket } from "./store/socket";
import { followRenderModes } from "./theme/metrics";
import { App } from "./views/App";

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

/** The window the page was opened to show alone (the pull-out-window spec, section 7.5), removed from the URL as
 *  it is read. */
function takeSoloWindowIdFromLocation(): string | null {
  const soloWindowId = parseSoloWindowId(window.location.search);
  if (soloWindowId !== null) stripLocationSearch(stripSoloParam);
  return soloWindowId;
}

/** The frame a reattach message names, when it names one: four finite fractions (clamped by the verb). */
function frameFromMessage(value: unknown): Frame | null {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const numbers = ["x", "y", "width", "height"].map((key) => record[key]);
  if (!numbers.every((number) => typeof number === "number" && Number.isFinite(number))) return null;
  const [x, y, width, height] = numbers as number[];
  return { x, y, width, height };
}

/** The shell's side of the pull-out conversation: every ask goes to the embedding chrome. */
const popOutBridge: PopOutBridge = {
  requestPopOut: (request) => sendToEmbedder(POP_OUT_WINDOW, { ...request }),
  cancelPopOut: (windowId) => sendToEmbedder(POP_OUT_CANCEL, { windowId }),
  endPopOut: (windowId) => sendToEmbedder(POP_OUT_END, { windowId }),
  reportDetachedWindows: (windows) => sendToEmbedder(DETACHED_WINDOWS, { windows: [...windows] }),
};

function bootstrap(): void {
  const clientId = getClientId();
  const soloWindowId = takeSoloWindowIdFromLocation();
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
          notify: (message) => alert(message),
          reloadInterface,
          popOut: popOutBridge,
          soloWindowId,
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
  // The child-frame boundary: the minds relay for the framed pages' `minds:` messages, and the
  // shell side of the app contract.
  initEmbedderRelay();
  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, () => void desktopStore.closeFocusedWindow());
  setEmbedderMessageHandler(FOCUS_CHAT, (message) => {
    const chatId = message.chatId;
    if (typeof chatId === "string" && chatId !== "") void desktopStore.focusChat(chatId);
  });
  // The pull-out conversation's two asks from the chrome: what it can do, and a window to bring back.
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    desktopStore.setCanPopOut(message.canPopOut === true);
  });
  setEmbedderMessageHandler(REATTACH_WINDOW, (message) => {
    const windowId = message.windowId;
    if (typeof windowId !== "string" || windowId === "") return;
    void desktopStore.reattachWindow(windowId, frameFromMessage(message.frame));
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
  // registered: a focus-chat ask needs the desktops and the apps. ``start`` reads both from the
  // inventory, but one whose inventory read failed returns without them, and the apps then land
  // with the socket's first ``apps_updated``; the embedder sends the ask the moment this lands.
  void Promise.all([started, desktopStore.whenAppsLoaded()]).then(() => announceReadyToEmbedder());
}

window.addEventListener("load", bootstrap);
