import m from "mithril";

/** The chat's terminal back face: a frame on the terminal app, with the flags a terminal page needs.
 *
 *  Its size and its corner are `.terminal-frame` in the stylesheet rather than inline here,
 *  because the corner is not a property of the frame on its own: it is the pane's corner minus
 *  the gutter `.chat-flip-back` holds it in, and the two have to be read together to stay
 *  concentric. */
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
