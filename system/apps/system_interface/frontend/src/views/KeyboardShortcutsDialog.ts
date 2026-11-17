/**
 * The keyboard shortcuts dialog (user-profile-state spec, section 10.3): the Keyboard settings
 * section in a modal of its own, opened from "Keyboard shortcuts..." on the backdrop's context
 * menu. Shortcuts are the viewer's, not a desktop's, so the dialog names no desktop and stays
 * available while viewing somebody else's.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { Modal } from "@imbue/workspace-ui/src/components/Modal";
import { KeyboardSettingsSection } from "./KeyboardSettingsSection";

export interface KeyboardShortcutsDialogAttrs {
  readonly onClose: () => void;
}

export const KEYBOARD_SHORTCUTS_DIALOG_WIDTH = 560;

export function KeyboardShortcutsDialog(): m.Component<KeyboardShortcutsDialogAttrs> {
  return {
    view(vnode) {
      const attrs = vnode.attrs;
      return m(
        Modal,
        {
          onDismiss: attrs.onClose,
          onEscape: attrs.onClose,
          title: "Keyboard shortcuts",
          width: KEYBOARD_SHORTCUTS_DIALOG_WIDTH,
          card: { "data-keyboard-shortcuts-dialog": true },
          actions: [m(Button, { extra: "keyboard-shortcuts-close", onclick: attrs.onClose }, "Close")],
        },
        [m(KeyboardSettingsSection)],
      );
    },
  };
}
