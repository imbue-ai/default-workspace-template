/**
 * Solo mode's one surface (the pull-out-window spec, section 7.5): the host the live-pages layer lays the
 * solo window's page over, filling the viewport. There is no backdrop, no chrome, and no taskbar: the chrome
 * that pulled the window out draws its own bar above this document. A window that is gone from the desktop
 * shows a short note instead of a page.
 */

import m from "mithril";
import { findWindow } from "../reducers/desktopState";
import type { DesktopStore } from "../store/DesktopStore";

export interface SoloViewAttrs {
  readonly store: DesktopStore;
  readonly windowId: string;
  /** The element the live pages are appended to, created once and never re-rendered. */
  readonly onPagesHostCreated: (host: HTMLElement) => void;
}

export const SoloView: m.Component<SoloViewAttrs> = {
  view(vnode) {
    const { store, windowId } = vnode.attrs;
    const state = store.getState();
    const isKnown = findWindow(state, windowId) !== null;
    return m(
      "div",
      {
        "data-solo-window": windowId,
        class: "solo-view relative min-h-0 flex-1 overflow-hidden",
      },
      [
        m("div", {
          class: "live-pages pointer-events-none absolute inset-0 [&>*]:pointer-events-auto",
          oncreate: (created: m.VnodeDOM) => vnode.attrs.onPagesHostCreated(created.dom as HTMLElement),
          onbeforeupdate: () => false,
        }),
        isKnown || !state.isDesktopsLoaded
          ? null
          : m(
              "div",
              {
                "data-solo-window-gone": "",
                class: "flex h-full items-center justify-center text-(length:--font-size-row) text-faint",
              },
              "This window is no longer open.",
            ),
      ],
    );
  },
};
