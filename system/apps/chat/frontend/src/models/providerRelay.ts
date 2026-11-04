/**
 * Handing a provider sign-in to the minds desktop app.
 *
 * A provider's browser sign-in calls back to a loopback port on the machine the browser runs
 * on, where nothing of this workspace listens. The desktop app can: asked with the sign-in's
 * page and flow, it listens on that port, relays the callback into this workspace's flow, and
 * opens the page in the user's chosen browser. It answers whether it is doing so. A plain
 * browser, or a chrome that cannot bind the port, answers no or not at all -- and then the
 * chooser offers a way to sign in that needs no relay. A port that is only taken by something
 * else on the user's machine is said apart, since the CLI draws a fresh one each run.
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

/** The desktop app is relaying; or the callback port the CLI drew is taken on the user's machine,
 *  which a fresh sign-in may clear; or it cannot relay. */
export type RelayAnswer = "relaying" | "port_taken" | "unavailable";

/** Settles the request still waiting on an ack. The ack has one handler slot, so only one request may own it. */
let settlePending: ((answer: RelayAnswer) => void) | null = null;

function answerFromAck(message: Record<string, unknown>): RelayAnswer {
  if (message.relay === true) return "relaying";
  return message.reason === "port_taken" ? "port_taken" : "unavailable";
}

/** Ask the desktop app to relay this sign-in and open its page; whether it will. */
export function requestProviderRelay(url: string, flowId: string): Promise<RelayAnswer> {
  // A page with no embedder at all has nobody to ask.
  if (typeof window !== "undefined" && window.parent === window) return Promise.resolve("unavailable");
  settlePending?.("unavailable");
  return new Promise((resolve) => {
    let isSettled = false;
    const settle = (answer: RelayAnswer): void => {
      if (isSettled) return;
      isSettled = true;
      if (settlePending === settle) settlePending = null;
      clearTimeout(timer);
      clearEmbedderMessageHandler(PROVIDER_SIGN_IN_ACK);
      resolve(answer);
    };
    settlePending = settle;
    const timer = setTimeout(() => settle("unavailable"), RELAY_ACK_TIMEOUT_MS);
    setEmbedderMessageHandler(PROVIDER_SIGN_IN_ACK, (message) => settle(answerFromAck(message)));
    sendToEmbedder(PROVIDER_SIGN_IN, { url, flowId });
  });
}

/** Tell the desktop app a sign-in it may be relaying has ended, so it stops listening for it. */
export function endProviderRelay(flowId: string): void {
  sendToEmbedder(PROVIDER_SIGN_IN_END, { flowId });
}
