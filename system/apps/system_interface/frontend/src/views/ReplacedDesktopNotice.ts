/**
 * The notice a visiting user sees when the desktop the shell had made for them is no longer among the workspace's
 * desktops, so a fresh one was seeded for them at arrival (plan section 3.10). It names no cause: the desktop may have
 * been deleted, or lost with every other when the workspace's desktops could not be read. Dismissed with one button.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";

export interface ReplacedDesktopNoticeAttrs {
  /** The name of the desktop that is gone. */
  readonly replacedDesktopName: string;
  /** The name of the desktop seeded in its place (not always where this client landed: a deep link wins). */
  readonly seededDesktopName: string;
  readonly onDismiss: () => void;
}

export const ReplacedDesktopNotice: m.Component<ReplacedDesktopNoticeAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      Modal,
      {
        onDismiss: attrs.onDismiss,
        onEscape: attrs.onDismiss,
        title: "Your desktop is gone",
        card: { "data-replaced-desktop-notice": attrs.replacedDesktopName },
        actions: m(Button, { variant: "primary", extra: "replaced-desktop-dismiss", onclick: attrs.onDismiss }, "OK"),
      },
      m("p", { class: MODAL_MESSAGE_CLASS }, [
        "Your desktop ",
        m("strong", attrs.replacedDesktopName),
        " is no longer in this workspace, so a fresh one, ",
        m("strong", attrs.seededDesktopName),
        ", was set up for you from the workspace's first desktop.",
      ]),
    );
  },
};
