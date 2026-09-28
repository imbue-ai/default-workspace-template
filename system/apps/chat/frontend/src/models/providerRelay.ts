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
  clearEmbedderMessageHandler,
  sendToEmbedder,
  setEmbedderMessageHandler,
} from "@imbue/workspace-ui/src/embed";

/** How long to wait for the desktop app to answer before signing in without it. */
export const RELAY_ACK_TIMEOUT_MS = 1000;

/** Ask the desktop app to relay this sign-in and open its page; whether it will. */
export function requestProviderRelay(url: string, flowId: string): Promise<boolean> {
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
