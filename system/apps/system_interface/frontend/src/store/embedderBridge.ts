/**
 * The desktop's side of what the Imbue Studio chrome says it can do and of immersive mode (the Screen
 * integration spec, section 5): the capabilities announcement feeds the store's capability flags, a
 * ``minds:immersive-changed`` lands in the store, and the store's immersive requests go out through the embed
 * module. Kept apart from the page's bootstrap so a test can drive it over a fake embedder.
 */

import {
  EMBEDDER_CAPABILITIES,
  IMMERSIVE_CHANGED,
  IMMERSIVE_REQUEST,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";
import type { DesktopStore, ImmersiveBridge } from "./DesktopStore";

/** The shell's side of the immersive conversation: every ask goes to the embedding chrome. */
export const immersiveBridge: ImmersiveBridge = {
  requestImmersive: (windowId, isOn) => sendToEmbedder(IMMERSIVE_REQUEST, { windowId, on: isOn }),
};

/** Register the handlers for the chrome's capabilities and its immersive-mode changes, feeding ``store``. */
export function connectEmbedderToStore(store: DesktopStore): void {
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, (message) => {
    store.setCanPopOut(message.canPopOut === true);
    store.setOutsideLinkCapabilities({
      opensExternalLinks: message.opensExternalLinks === true,
      opensCustomSchemeLinks: message.opensCustomSchemeLinks === true,
    });
    store.setOpensLocalFiles(message.opensLocalFiles === true);
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
