/**
 * The notice a visiting user sees when the desktop the shell had made for them was deleted since their last visit,
 * so a fresh one was seeded for them at arrival (plan section 3.10). Dismissed with one button.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";

export interface ReplacedDesktopNoticeAttrs {
  /** The name of the desktop that was deleted. */
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
        title: "Your desktop was deleted",
        card: { "data-replaced-desktop-notice": attrs.replacedDesktopName },
        actions: m(Button, { variant: "primary", extra: "replaced-desktop-dismiss", onclick: attrs.onDismiss }, "OK"),
      },
      m("p", { class: MODAL_MESSAGE_CLASS }, [
        "Your desktop ",
        m("strong", attrs.replacedDesktopName),
        " was deleted since your last visit, so a fresh one, ",
        m("strong", attrs.seededDesktopName),
        ", was set up for you from the workspace's first desktop.",
      ]),
    );
  },
};
