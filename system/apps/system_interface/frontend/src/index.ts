import { getBasePath } from "./base-path";
import { initAgentManager } from "./models/AgentManager";
import { initEmbedderRelay } from "./relay";
import m from "mithril";
import "./style.css";
import { App } from "./views/App";

function getEffectiveRoutePrefix(): string {
  // When served through the desktop client proxy, the <base> tag contains
  // the forwarding prefix (e.g., /forwarding/{agentId}/web/). Use this as
  // the Mithril route prefix so pushState preserves the correct URL in the
  // browser history stack (enabling back/forward navigation).
  const baseEl = document.querySelector("base[href]");
  if (baseEl) {
    const href = baseEl.getAttribute("href") ?? "";
    if (href.includes("/forwarding/")) {
      return href.replace(/\/+$/, "");
    }
  }
  return getBasePath();
}

function bootstrap(): void {
  m.route.prefix = getEffectiveRoutePrefix();
  initAgentManager();
  // The child-frame boundary: the minds relay for the chat pages' permission cards, and the
  // shell side of the app contract.
  initEmbedderRelay();
  const rootElement = document.getElementById("app");
  if (rootElement) {
    const appResolver: m.RouteResolver = { render: () => m(App) };
    m.route(rootElement, "/", { "/": appResolver });
  }
}

window.addEventListener("load", bootstrap);
