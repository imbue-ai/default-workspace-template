/**
 * Handing a provider sign-in to the minds desktop app.
 *
 * A provider's browser sign-in calls back to a loopback port on the machine the browser runs
 * on, where nothing of this workspace listens. The desktop app can: asked with the sign-in's
 * page and flow, it listens on that port, relays the callback into this workspace's flow, and
 * opens the page in the user's chosen browser. It answers whether it is doing so. A plain
 * browser, or a chrome that cannot bind the port, answers no or not at all -- and then the
 * chooser offers a way to sign in that needs no relay.
 */

import {
  PROVIDER_SIGN_IN,
  PROVIDER_SIGN_IN_ACK,
  PROVIDER_SIGN_IN_END,
  clearEmbedderMessageHandler,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";

/** How long to wait for an embedder to answer before signing in without it. Only a chrome that
 *  predates the relay stays silent; the current ones answer as soon as the page is opened. */
export const RELAY_ACK_TIMEOUT_MS = 5000;

/** Ask the desktop app to relay this sign-in and open its page; whether it will. */
export function requestProviderRelay(url: string, flowId: string): Promise<boolean> {
  // A page with no embedder at all has nobody to ask.
  if (typeof window !== "undefined" && window.parent === window) return Promise.resolve(false);
  return new Promise((resolve) => {
    let isSettled = false;
    const settle = (isRelaying: boolean): void => {
      if (isSettled) return;
      isSettled = true;
      clearTimeout(timer);
      clearEmbedderMessageHandler(PROVIDER_SIGN_IN_ACK);
      resolve(isRelaying);
    };
    const timer = setTimeout(() => settle(false), RELAY_ACK_TIMEOUT_MS);
    setEmbedderMessageHandler(PROVIDER_SIGN_IN_ACK, (message) => settle(message.relay === true));
    sendToEmbedder(PROVIDER_SIGN_IN, { url, flowId });
  });
}

/** Tell the desktop app a sign-in it may be relaying has ended, so it stops listening for it. */
export function endProviderRelay(flowId: string): void {
  sendToEmbedder(PROVIDER_SIGN_IN_END, { flowId });
}
