import m from "mithril";

/** Renders its children into <body>.
 *
 * Popovers here live inside dockview's `overflow: hidden` panels, so anything that extends
 * past its panel is clipped at the edge. Mithril has no portal, so this mounts a detached root
 * and renders into it -- the same shape `lightbox.ts` and `hoverTooltip.ts` use.
 *
 * Children must be positioned in VIEWPORT coordinates (`position: fixed`): they no longer have
 * their original parent to lay them out.
 *
 * The listeners below are the whole reason this file has a comment. Mithril only wires
 * auto-redraw into event handlers for roots created by `m.mount` / `m.route`; `m.render` is
 * documented as the manual API and deliberately does NOT. Without them every handler inside a
 * portalled popover mutates state and then nothing re-renders -- a row click sets the flyout
 * and no flyout appears, a trash click arms "Remove?" and the icon does not change, and the
 * next click outside tears the whole thing down, which is what the user actually sees. So the
 * host re-adds what `m.mount` would have: a redraw after any interaction inside it. Bubble
 * phase, so the target's own handler has already run.
 */
export function Portal(): m.Component<{ children: m.Children }> {
  let host: HTMLElement | null = null;
  const redraw = (): void => {
    m.redraw();
  };
  return {
    onremove() {
      if (host !== null) {
        for (const type of REDRAW_EVENTS) host.removeEventListener(type, redraw);
        m.render(host, null);
        host.remove();
        host = null;
      }
    },
    view(vnode) {
      // Created here rather than in `oncreate`: the view runs first, so a host made there
      // would be empty on the pass that mattered and nothing would schedule another.
      if (host === null) {
        host = document.createElement("div");
        document.body.appendChild(host);
        for (const type of REDRAW_EVENTS) host.addEventListener(type, redraw);
      }
      m.render(host, vnode.attrs.children);
      return null;
    },
  };
}

/** Every event that can change what a portalled popover should show. */
const REDRAW_EVENTS = ["click", "input", "change", "keyup"] as const;
