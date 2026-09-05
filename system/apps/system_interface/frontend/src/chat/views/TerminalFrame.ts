import m from "mithril";

/** The chat's terminal back face: a plain ttyd frame, with the flags a terminal page needs. */
export const TerminalFrame: m.Component<{ url: string; title: string }> = {
  view(vnode) {
    return m("iframe", {
      src: vnode.attrs.url,
      title: vnode.attrs.title,
      style: "width: 100%; height: 100%; border: none;",
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
      allow: "clipboard-read; clipboard-write",
    });
  },
};
