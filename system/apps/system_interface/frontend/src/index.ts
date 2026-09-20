import m from "mithril";
import { getClientId } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import { setEmbedderMessageHandler } from "@imbue/workspace-ui/src/embed";
import "./style.css";
import * as api from "./model/api";
import { isDeepLinkEmpty, parseDeepLink, stripDeepLinkParams } from "./model/deepLinks";
import type { DeepLink } from "./model/deepLinks";
import { PointerGestureSource } from "./gestures/pointerGestures";
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
  // The child-frame boundary: the minds relay for the framed pages' `minds:` messages, and the
  // shell side of the app contract.
  initEmbedderRelay();
  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, () => void desktopStore.closeFocusedWindow());
  const rootElement = document.getElementById("app");
  if (rootElement) {
    m.mount(rootElement, {
      view: () =>
        m(App, {
          store: desktopStore,
          gestures: new PointerGestureSource(),
          host: window.location.host,
          protocol: window.location.protocol,
        }),
    });
  }
  void desktopStore.start(takeDeepLinkFromLocation());
}

window.addEventListener("load", bootstrap);
