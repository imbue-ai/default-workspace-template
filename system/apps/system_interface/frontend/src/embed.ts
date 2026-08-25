/**
 * The workspace's single connection to the embedding minds chrome.
 *
 * All postMessage traffic with the embedder flows through the vendored minds
 * embed contract (see `@minds/embed-contract` and minds'
 * `docs/embed-contract.md`); this module owns the one workspace-side endpoint
 * and hands out narrow send/subscribe helpers. Raw `postMessage` /
 * `message`-listener usage anywhere else in this app is forbidden by the
 * system_interface ratchet suite, so the whole boundary stays auditable here.
 *
 * The page behaves identically embedded (iframe under the minds chrome) and
 * top-level (a direct share visit): with no embedder, outbound sends simply
 * have no listener and no embedder message ever arrives.
 */

import {
  CLOSE_ACTIVE_TAB,
  OPEN_AI_KEYS_ACK,
  createWorkspaceEndpoint,
  type ContractEndpoint,
  type ContractMessage,
} from "@minds/embed-contract";
import * as embedContract from "@minds/embed-contract";

// These types postdate the vendored embed_contract snapshot (they arrive with the next mngr
// release sync; this repo deliberately does not edit system/vendor by hand). A named import of
// a missing export fails the rollup build, so probe the namespace and fall back to the literal.
// Until the sync lands, each side simply drops the (to it) unknown type before any handler
// runs; the moment the sync lands the feature goes live with no code change here.
export const PERMISSION_REQUEST_RESOLVED: "minds:permission-request-resolved" =
  "PERMISSION_REQUEST_RESOLVED" in embedContract
    ? embedContract.PERMISSION_REQUEST_RESOLVED
    : "minds:permission-request-resolved";
// Workspace -> embedder: open the minds shell's Share tab focused on one app
// (payload { serviceName }). Fire-and-forget, like OPEN_REQUEST_MODAL: with no
// embedder present the send simply has no listener.
export const OPEN_SHARE_SETTINGS: "minds:open-share-settings" =
  "OPEN_SHARE_SETTINGS" in embedContract ? embedContract.OPEN_SHARE_SETTINGS : "minds:open-share-settings";

type EmbedderMessageHandler = (message: ContractMessage) => void;

// One replaceable handler per embedder->workspace type, registered by the
// feature that owns it (dockview registers close-active-tab at boot; the
// permission cards register the resolution relay at boot; the Claude sign-in
// modal registers/clears the mint ack around its handshake).
const handlerByType: Partial<Record<string, EmbedderMessageHandler>> = {};

// Created on first use rather than at import time so importing this module
// never touches `window` (unit tests run under node and stub it per test).
let endpoint: ContractEndpoint | null = null;

// A do-nothing endpoint for non-browser contexts: component unit tests run
// under node without a `window`, and features send/subscribe unconditionally.
const NULL_ENDPOINT: ContractEndpoint = {
  send: () => undefined,
  dispose: () => undefined,
};

function getEndpoint(): ContractEndpoint {
  if (endpoint === null) {
    if (typeof window === "undefined") return NULL_ENDPOINT;
    endpoint = createWorkspaceEndpoint({
      handlers: {
        [CLOSE_ACTIVE_TAB]: (message) => handlerByType[CLOSE_ACTIVE_TAB]?.(message),
        [OPEN_AI_KEYS_ACK]: (message) => handlerByType[OPEN_AI_KEYS_ACK]?.(message),
        [PERMISSION_REQUEST_RESOLVED]: (message) => handlerByType[PERMISSION_REQUEST_RESOLVED]?.(message),
      },
    });
  }
  return endpoint;
}

/** Send a workspace->embedder contract message (no-op when not embedded). */
export function sendToEmbedder(type: string, payload?: Record<string, unknown>): void {
  getEndpoint().send(type, payload);
}

/** Register the handler for one embedder->workspace type (replaces any prior one). */
export function setEmbedderMessageHandler(type: string, handler: EmbedderMessageHandler): void {
  getEndpoint();
  handlerByType[type] = handler;
}

/** Clear the handler for one embedder->workspace type. */
export function clearEmbedderMessageHandler(type: string): void {
  delete handlerByType[type];
}

/** Tear the endpoint down so the next use rebinds to the current `window`. Test-only. */
export function resetEmbedEndpointForTesting(): void {
  if (endpoint !== null) {
    endpoint.dispose();
    endpoint = null;
  }
  for (const type of Object.keys(handlerByType)) delete handlerByType[type];
}
