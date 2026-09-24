/**
 * Mounting a view for a jsdom test: a fresh root on the body per mount, torn down together by
 * ``unmountViews`` (a test file's ``afterEach``, and a test's own step before it renders again).
 * Kept apart from ``dom.ts``, which must not import mithril: it polyfills the globals mithril
 * reads when it is first evaluated.
 */

import m from "mithril";

const mountedRoots: HTMLElement[] = [];

/** Mount ``view`` into a new root appended to the body; answers the root. */
export function mountView(view: () => m.Children): HTMLElement {
  const root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view });
  mountedRoots.push(root);
  return root;
}

/** Unmount every view ``mountView`` mounted and remove its root. */
export function unmountViews(): void {
  for (const root of mountedRoots.splice(0)) {
    m.mount(root, null);
    root.remove();
  }
}
