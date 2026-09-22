import m from "mithril";

/** The chat's terminal back face: a frame on the terminal app, with the flags a terminal page needs.
 *  Its size and corner are `.terminal-frame` in the stylesheet. */
export const TerminalFrame: m.Component<{ url: string; title: string }> = {
  view(vnode) {
    return m("iframe", {
      src: vnode.attrs.url,
      title: vnode.attrs.title,
      class: "terminal-frame",
      sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
      allow: "clipboard-read; clipboard-write",
    });
  },
};
