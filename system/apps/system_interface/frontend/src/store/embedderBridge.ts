/**
 * The desktop's side of what the Imbue Studio chrome says it can do and of immersive mode (the Screen
 * integration spec, section 5): the capabilities announcement feeds the store's capability flags, the shortcuts the
 * chrome runs for the workspace (``minds:run-shortcut``, ``minds:run-chord``) reach the store, a
 * ``minds:immersive-changed`` lands in the store, and the store's immersive requests go out through the embed
 * module. Kept apart from the page's bootstrap so a test can drive it over a fake embedder.
 */

import {
  EMBEDDER_CAPABILITIES,
  IMMERSIVE_CHANGED,
  IMMERSIVE_REQUEST,
  OPEN_KEYBOARD_SHORTCUTS,
  RUN_CHORD,
  RUN_SHORTCUT,
  SHELL_SHORTCUTS,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";
import { CLOSE_ACTIVE_TAB } from "@minds/embed-contract";
import { isWorkspaceActionId } from "../model/keybindings";
import { isShortcutRecording } from "../model/shortcutRecording";
import type { DesktopStore, ImmersiveBridge, ShortcutsBridge } from "./DesktopStore";

/** The shell's side of the immersive conversation: every ask goes to the embedding chrome. */
export const immersiveBridge: ImmersiveBridge = {
  requestImmersive: (windowId, isOn) => sendToEmbedder(IMMERSIVE_REQUEST, { windowId, on: isOn }),
};

/** The shell's side of the keyboard shortcuts conversation: every ask goes to the embedding chrome. */
export const shortcutsBridge: ShortcutsBridge = {
  openKeyboardShortcuts: () => sendToEmbedder(OPEN_KEYBOARD_SHORTCUTS),
  reportShellShortcuts: (chordsByAction, claimedChords) =>
    sendToEmbedder(SHELL_SHORTCUTS, { chords_by_action: { ...chordsByAction }, claimed_chords: [...claimedChords] }),
};

/** An older chrome's close-tab chord (one that runs no workspace-wide shortcut of its own): close the focused window,
 *  unless the shell's own shortcut recorder is listening for keys, which such a chrome does not know. */
export function closeFocusedWindowUnlessRecording(closeFocusedWindow: () => void): void {
  if (isShortcutRecording()) return;
  closeFocusedWindow();
}

/** Register the handlers for the chrome's capabilities, the shortcuts it runs for the workspace, and its
 *  immersive-mode changes, feeding ``store``. */
export function connectEmbedderToStore(store: DesktopStore): void {
  setEmbedderMessageHandler(CLOSE_ACTIVE_TAB, () =>
    closeFocusedWindowUnlessRecording(() => void store.closeFocusedWindow()),
  );
  setEmbedderMessageHandler(RUN_SHORTCUT, (message) => {
    if (isWorkspaceActionId(message.action)) store.runWorkspaceAction(message.action);
  });
  setEmbedderMessageHandler(RUN_CHORD, (message) => {
    if (typeof message.chord === "string" && message.chord !== "") store.forwardChordToFocusedWindow(message.chord);
  });
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    store.setCanPopOut(message.canPopOut === true);
    store.setOutsideLinkCapabilities({
      opensExternalLinks: message.opensExternalLinks === true,
      opensCustomSchemeLinks: message.opensCustomSchemeLinks === true,
    });
    store.setOpensLocalFiles(message.opensLocalFiles === true);
    store.setOpensKeyboardShortcuts(message.opensKeyboardShortcuts === true);
    store.setCanImmerse(message.immersive === true);
    store.setEmbedderPresent(true);
  });
  setEmbedderMessageHandler(IMMERSIVE_CHANGED, (message) => {
    store.onImmersiveChanged({
      isOn: message.on === true,
      isKeyboardLocked: message.keyboardLocked === true,
      error: typeof message.error === "string" && message.error !== "" ? message.error : null,
    });
  });
}
