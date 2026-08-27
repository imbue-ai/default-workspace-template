import m from "mithril";

/** Renders its children into <body>.
 *
 * Popovers here live inside dockview's `overflow: hidden` panels, so anything that extends
 * past its panel is clipped at the edge. Mithril has no portal, so this mounts a detached root
 * and renders into it -- the same shape `lightbox.ts` and `hoverTooltip.ts` use.
 *
 * Children must be positioned in VIEWPORT coordinates (`position: fixed`): they no longer have
 * their original parent to lay them out.
 */
export function Portal(): m.Component<{ children: m.Children }> {
  let host: HTMLElement | null = null;
  return {
    onremove() {
      if (host !== null) {
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
      }
      m.render(host, vnode.attrs.children);
      return null;
    },
  };
}
