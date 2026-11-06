import m from "mithril";

/** The chat's terminal back face: a frame on the terminal app, with the flags a terminal page needs.
 *  Its size and corner are `.terminal-frame` in the stylesheet.
 *
 *  The terminal-styled line sits under the frame for good, so whenever the frame paints nothing --
 *  before it loads, and the frame or two Chromium takes to draw a cross-origin frame it had hidden --
 *  what shows is the terminal's surface, not the card's. The frame stays hidden until its `load`,
 *  which waits for the pty page it nests. It keeps its size while hidden: ttyd fits its grid to the
 *  viewport it loads into. */
export function TerminalFrame(): m.Component<{ url: string; title: string }> {
  let loadedUrl: string | null = null;
  return {
    view(vnode) {
      const isLoaded = loadedUrl === vnode.attrs.url;
      return m("div", { class: "terminal-frame-box" }, [
        m("div", { class: "terminal-frame-loading terminal-placeholder" }, "Opening the terminal..."),
        m("iframe", {
          src: vnode.attrs.url,
          title: vnode.attrs.title,
          class: isLoaded ? "terminal-frame" : "terminal-frame terminal-frame-pending",
          sandbox: "allow-scripts allow-same-origin allow-forms allow-popups",
          allow: "clipboard-read; clipboard-write",
          onload: () => {
            loadedUrl = vnode.attrs.url;
          },
        }),
      ]);
    },
  };
}
