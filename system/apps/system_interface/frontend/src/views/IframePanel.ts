import m from "mithril";
import { apiUrl } from "../base-path";
import { getApps } from "../models/AgentManager";
import type { AppEntry } from "../models/AgentManager";
import { appStoppedDetail, isAppStoppable, stoppedAppForServiceName } from "../models/appLiveness";
import { displayNameForMember } from "../models/MemberTitles";
import { memberRef } from "../models/Projects";
import { Button } from "./Button";
import { appServiceDisplayName } from "./derived-names";

interface IframePanelAttrs {
  url: string;
  title: string;
  serviceName?: string;
  /** The identity of the live page this iframe *is*, machine-wide. There is
   *  one of these per object rather than one per pane, which is what lets the
   *  per-tab Refresh find the right iframe without knowing which pane (or
   *  which project) is showing it. */
  liveKey?: string;
}

export const IFRAME_PANEL_SERVICE_NAME_ATTR = "data-service-name";
export const IFRAME_PANEL_LIVE_KEY_ATTR = "data-live-key";

export const IframePanel: m.Component<IframePanelAttrs> = {
  view(vnode) {
    const { url, title, serviceName, liveKey } = vnode.attrs;
    // A stopped app's pane shows a lightweight placeholder instead of the dead
    // iframe's raw connection error. Rendered per redraw off the live app list,
    // so a start (from anywhere) swaps the iframe back in on the next
    // `apps_updated` push. Only a service-backed pane can be stopped this way;
    // ad-hoc URL panes and the fleets keep their iframe.
    const stoppedApp = stoppedAppForServiceName(getApps(), serviceName ?? null);
    if (stoppedApp !== null) {
      return m(StoppedAppPlaceholder, { app: stoppedApp });
    }
    const attrs: Record<string, string> = {
      src: url,
      title,
      style: "width: 100%; height: 100%; border: none;",
      // Service panels are cross-origin iframes (each service owns its own
      // origin), so allow-same-origin only lets the framed app be a normal
      // page on ITS origin -- it grants nothing on the shell's origin.
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
      // Let embedded services (e.g. the browser fleet viewer) reach the user's
      // clipboard via navigator.clipboard for copy/paste into the remote browser.
      allow: "clipboard-read; clipboard-write",
    };
    if (serviceName) {
      attrs[IFRAME_PANEL_SERVICE_NAME_ATTR] = serviceName;
    }
    if (liveKey) {
      attrs[IFRAME_PANEL_LIVE_KEY_ATTR] = liveKey;
    }
    return m("iframe", attrs);
  },
};

/**
 * The minimal stopped-tab state: what the pane is, that it is stopped, and
 * nothing else -- deliberately no log tail and no diagnostics, since Stop is a
 * reversible, ordinary state rather than an error. The Start button appears
 * only where the app is startable through the workspace (a supervised,
 * non-essential app); the `apps_updated` push after a start swaps the iframe
 * back in.
 */
const StoppedAppPlaceholder: m.Component<{ app: AppEntry }> = {
  view(vnode) {
    const app = vnode.attrs.app;
    const label = displayNameForMember(memberRef("app", app.name), appServiceDisplayName(app.name));
    return m(
      "div",
      {
        class: "si-stopped-app flex h-full w-full flex-col items-center justify-center gap-3 bg-surface",
        "data-service-name": app.name,
      },
      [
        m("div", { class: "text-[15px] font-medium text-primary" }, label),
        m("div", { class: "text-[13px] text-faint" }, appStoppedDetail(app)),
        isAppStoppable(app)
          ? m(
              Button,
              {
                extra: "si-stopped-app-start mt-1",
                onclick: () => {
                  // The `apps_updated` push is the authority on success; a
                  // failed request leaves the placeholder (and the button, for
                  // a retry) in place, logged rather than alerted -- this pane
                  // is deliberately minimal.
                  void fetch(apiUrl(`/api/apps/${encodeURIComponent(app.name)}/start`), { method: "POST" })
                    .then(async (response) => {
                      if (response.ok) return;
                      const data = (await response.json().catch(() => ({}))) as { detail?: string };
                      console.warn(`Failed to start ${app.name}: ${data.detail ?? `HTTP ${response.status}`}`);
                    })
                    .catch((e: Error) => {
                      console.warn(`Failed to start ${app.name}: ${e.message}`);
                    });
                },
              },
              `Start ${label}`,
            )
          : null,
      ],
    );
  },
};

/** Reload every iframe tagged with data-service-name===serviceName.
 *
 *  Service panels are cross-origin iframes (each service owns its own
 *  origin), so reading contentWindow.location throws a SecurityError and the
 *  src-reassignment fallback is the normal path. The
 *  contentWindow.location.reload() branch is kept for the rare same-origin
 *  iframe. Used by both the per-tab refresh button and the WS-driven
 *  agent-triggered refresh.
 *
 *  Every pane on the service reloads, backgrounded ones included: a page stays
 *  live whether or not a view is showing it, and "refresh the app" has always
 *  meant the app rather than this pane. */
export function reloadIframesForService(serviceName: string): number {
  const iframes = document.querySelectorAll<HTMLIFrameElement>(
    `iframe[${IFRAME_PANEL_SERVICE_NAME_ATTR}="${CSS.escape(serviceName)}"]`,
  );
  iframes.forEach((iframe) => {
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
  });
  return iframes.length;
}
