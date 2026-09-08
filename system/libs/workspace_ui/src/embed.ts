/**
 * The workspace's single connection to the embedding minds chrome.
 *
 * All postMessage traffic with the embedder flows through the vendored minds
 * embed contract (see `@minds/embed-contract` and minds'
 * `docs/embed-contract.md`); this module owns the one workspace-side endpoint
 * and hands out narrow send/subscribe helpers. Raw `postMessage` /
 * `message`-listener usage anywhere else is forbidden by each frontend's embed
 * ratchets, so the whole boundary stays auditable here.
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

// A named import of an export the vendored embed_contract snapshot lacks fails
// the rollup build (this repo does not edit system/vendor by hand; the snapshot
// moves with the mngr release sync), so these probe the namespace and fall
// back to the literal. A snapshot without the export drops the type it does
// not know at its validator.
export const PERMISSION_RESOLUTIONS: "minds:permission-resolutions" =
  "PERMISSION_RESOLUTIONS" in embedContract ? embedContract.PERMISSION_RESOLUTIONS : "minds:permission-resolutions";
// Workspace -> embedder: open the minds shell's Share tab focused on one app.
// Payload: { serviceName }.
export const OPEN_SHARE_SETTINGS: "minds:open-share-settings" =
  "OPEN_SHARE_SETTINGS" in embedContract ? embedContract.OPEN_SHARE_SETTINGS : "minds:open-share-settings";

type EmbedderMessageHandler = (message: ContractMessage) => void;

// One replaceable handler per embedder->workspace type, registered by the
// feature that owns it.
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
        [PERMISSION_RESOLUTIONS]: (message) => handlerByType[PERMISSION_RESOLUTIONS]?.(message),
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
