import m from "mithril";

interface IframePanelAttrs {
  url: string;
  title: string;
  serviceName?: string;
  panelId?: string;
}

export const IFRAME_PANEL_SERVICE_NAME_ATTR = "data-service-name";
export const IFRAME_PANEL_PANEL_ID_ATTR = "data-panel-id";

export const IframePanel: m.Component<IframePanelAttrs> = {
  view(vnode) {
    const { url, title, serviceName, panelId } = vnode.attrs;
    const attrs: Record<string, string> = {
      src: url,
      title,
      style: "width: 100%; height: 100%; border: none;",
      // Service panels are cross-origin iframes (each service owns its own
      // origin), so allow-same-origin only lets the framed app be a normal
      // page on ITS origin -- it grants nothing on the shell's origin.
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups allow-downloads",
      // Let embedded services (e.g. the browser fleet viewer) reach the user's
      // clipboard via navigator.clipboard for copy/paste into the remote browser.
      allow: "clipboard-read; clipboard-write",
    };
    if (serviceName) {
      attrs[IFRAME_PANEL_SERVICE_NAME_ATTR] = serviceName;
    }
    if (panelId) {
      attrs[IFRAME_PANEL_PANEL_ID_ATTR] = panelId;
    }
    return m("iframe", attrs);
  },
};

/** Reload every iframe tagged with data-service-name===serviceName.
 *
 *  Service panels are cross-origin iframes (each service owns its own
 *  origin), so reading contentWindow.location throws a SecurityError and the
 *  src-reassignment fallback is the normal path. The
 *  contentWindow.location.reload() branch is kept for the rare same-origin
 *  iframe. Used by both the per-tab refresh button and the WS-driven
 *  agent-triggered refresh. */
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
