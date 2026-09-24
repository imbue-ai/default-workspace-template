import m from "mithril";
import { getClientId } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import { FOCUS_CHAT, announceReadyToEmbedder, setEmbedderMessageHandler } from "@imbue/workspace-ui/src/embed";
import "./style.css";
import * as api from "./model/api";
import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./model/deepLinks";
import type { DeepLink } from "./model/deepLinks";
import { PointerGestureSource } from "./gestures/pointerGestures";
import { startPresenceHeartbeat } from "./model/Presence";
import { initEmbedderRelay } from "./relay";
import { reloadInterface } from "./reload";
import { DesktopStore } from "./store/DesktopStore";
import { ShellSocket } from "./store/socket";
import { followRenderModes } from "./theme/metrics";
import { App } from "./views/App";

/** The deep link the page was opened with (contracts.md section 9), removed from the URL as it is read. */
function takeDeepLinkFromLocation(): DeepLink {
  const link = parseDeepLink(window.location.search);
  if (isDeepLinkEmpty(link)) return link;
  const stripped = `${window.location.pathname}${stripDeepLinkParams(window.location.search)}${window.location.hash}`;
  window.history.replaceState(window.history.state, "", stripped);
  return link;
}

function bootstrap(): void {
  const clientId = getClientId();
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
