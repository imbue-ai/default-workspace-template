import m from "mithril";

interface IframePanelAttrs {
  url: string;
  title: string;
  serviceName?: string;
  panelId?: string;
  // Set when ``serviceName`` resolves back to the instance serving this shell
  // (see ``getSelfReferentialServices``). The panel then explains itself
  // instead of framing anything -- see ``renderSelfReferentialNotice``.
  isSelfReferential?: boolean;
}

export const IFRAME_PANEL_SERVICE_NAME_ATTR = "data-service-name";
export const IFRAME_PANEL_PANEL_ID_ATTR = "data-panel-id";

/**
 * The panel body shown in place of a service that resolves back to this shell.
 *
 * Framing it would nest this instance inside itself, so the tab gets this
 * instead. It deliberately explains *why* the tab is not showing what the user
 * expects: a blank or erroring tab sitting in the middle of an otherwise
 * faithful layout reads as the preview being broken. There is no retry --
 * nothing about this resolves by waiting.
 */
export function renderSelfReferentialNotice(serviceName: string): m.Vnode {
  return m(
    "div",
    {
      class: "iframe-panel-self-referential",
      style:
        "display: flex; flex-direction: column; justify-content: center; align-items: center; " +
        "width: 100%; height: 100%; padding: 0 24px; text-align: center; " +
        "color: var(--color-text-secondary); background: var(--color-bg);",
    },
    [
      m(
        "p",
        { style: "margin: 0; max-width: 32rem; line-height: 1.5;" },
        "This tab is the preview you are already looking at.",
      ),
      m(
        "p",
        {
          style:
            "margin: 10px 0 0; max-width: 32rem; line-height: 1.5; font-size: 14px; color: var(--color-text-faint);",
        },
        [
          "Opening ",
          m("code", serviceName),
          " in here would nest the preview inside itself, so it is left out. Every other tab is the real thing.",
        ],
      ),
    ],
  );
}

export const IframePanel: m.Component<IframePanelAttrs> = {
  view(vnode) {
    const { url, title, serviceName, panelId, isSelfReferential } = vnode.attrs;
    if (isSelfReferential === true && serviceName !== undefined) {
      return renderSelfReferentialNotice(serviceName);
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
