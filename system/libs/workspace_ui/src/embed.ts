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

// A named import of an export the embed_contract snapshot lacks fails
// the rollup build (this repo does not edit system/vendor by hand; the snapshot
// moves with mngr), so these probe the namespace and fall
// back to the literal. A snapshot without the export drops the type it does
// not know at its validator.
export const PERMISSION_RESOLUTIONS: "minds:permission-resolutions" =
  "PERMISSION_RESOLUTIONS" in embedContract ? embedContract.PERMISSION_RESOLUTIONS : "minds:permission-resolutions";
// Workspace -> embedder: open the minds shell's Share tab focused on one app.
// Payload: { serviceName }.
export const OPEN_SHARE_SETTINGS: "minds:open-share-settings" =
  "OPEN_SHARE_SETTINGS" in embedContract ? embedContract.OPEN_SHARE_SETTINGS : "minds:open-share-settings";
// Embedder -> workspace: the user opened a chat's notification in the minds
// shell; show that chat. Payload: { chatId } (the chat's id, which is its
// first agent's id). No page handles it here: the shell relays it to the app
// whose manifest registers the type.
const FOCUS_CHAT: "minds:focus-chat" = "FOCUS_CHAT" in embedContract ? embedContract.FOCUS_CHAT : "minds:focus-chat";
// Workspace -> embedder: this page's endpoint is listening. Payload: {}. The
// embedder holds a focus-chat ask until it arrives, rather than guessing when
// a freshly-mounted frame's page is live.
export const WORKSPACE_READY: "minds:workspace-ready" =
  "WORKSPACE_READY" in embedContract ? embedContract.WORKSPACE_READY : "minds:workspace-ready";
// The pull-out window set (contract v6, the pull-out-window spec), probed the same way.
// Workspace -> embedder: show one pulled-out window in a desktop window of the chrome's own, opened
// if it has none. Payload: { windowId, title, width, height } (the window's rendered size in CSS px).
export const POP_OUT_WINDOW: "minds:pop-out-window" =
  "POP_OUT_WINDOW" in embedContract ? embedContract.POP_OUT_WINDOW : "minds:pop-out-window";
// Workspace -> embedder: a title-bar drag began; the chrome watches the cursor from here and pulls
// the window out once it leaves the chrome's window. Payload: { windowId, title, width, height,
// grabX, grabY } (the size the window renders at, and where inside it the pointer holds it).
export const WINDOW_DRAG_STARTED: "minds:window-drag-started" =
  "WINDOW_DRAG_STARTED" in embedContract ? embedContract.WINDOW_DRAG_STARTED : "minds:window-drag-started";
// Workspace -> embedder: the shell's own end of a watched drag (a release it saw, or Escape).
// Payload: { windowId, isDetached }.
export const WINDOW_DRAG_ENDED: "minds:window-drag-ended" =
  "WINDOW_DRAG_ENDED" in embedContract ? embedContract.WINDOW_DRAG_ENDED : "minds:window-drag-ended";
// Workspace -> embedder: the pulled-out windows of this shell's active desktop, with their titles.
// Payload: { windows: [{ windowId, title }] }.
export const DETACHED_WINDOWS: "minds:detached-windows" =
  "DETACHED_WINDOWS" in embedContract ? embedContract.DETACHED_WINDOWS : "minds:detached-windows";
// Embedder -> workspace: what the chrome can do, right after WORKSPACE_READY. Payload: { canPopOut }.
export const EMBEDDER_CAPABILITIES: "minds:embedder-capabilities" =
  "EMBEDDER_CAPABILITIES" in embedContract ? embedContract.EMBEDDER_CAPABILITIES : "minds:embedder-capabilities";
// Embedder -> workspace: return a pulled-out window to the desktop. Payload: { windowId, frame? }.
export const REATTACH_WINDOW: "minds:reattach-window" =
  "REATTACH_WINDOW" in embedContract ? embedContract.REATTACH_WINDOW : "minds:reattach-window";
// Embedder -> workspace: a step of a watched drag: the cursor left the chrome's window and its own
// desktop window follows it, came back inside, or was released out there.
// Payload: { windowId, phase: "out" | "in" | "released" }.
export const TEAR_OUT: "minds:tear-out" = "TEAR_OUT" in embedContract ? embedContract.TEAR_OUT : "minds:tear-out";

type EmbedderMessageHandler = (message: ContractMessage) => void;

// Every embedder->workspace type the contract defines: the endpoint dispatches
// only the types it is handed a handler for, and each of these reaches its own
// handler and then the observer.
const EMBEDDER_TO_WORKSPACE_TYPES: readonly string[] = [
  CLOSE_ACTIVE_TAB,
  OPEN_AI_KEYS_ACK,
  PERMISSION_RESOLUTIONS,
  FOCUS_CHAT,
  EMBEDDER_CAPABILITIES,
  REATTACH_WINDOW,
  TEAR_OUT,
];

// One replaceable handler per embedder->workspace type, registered by the
// feature that owns it.
const handlerByType: Partial<Record<string, EmbedderMessageHandler>> = {};

// One replaceable observer of every embedder->workspace message, whatever its
// type: the shell's relay to the apps registered for it.
let messageObserver: EmbedderMessageHandler | null = null;

function dispatchEmbedderMessage(message: ContractMessage): void {
  handlerByType[message.type]?.(message);
  messageObserver?.(message);
}

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
      handlers: Object.fromEntries(EMBEDDER_TO_WORKSPACE_TYPES.map((type) => [type, dispatchEmbedderMessage])),
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

/** Observe every embedder->workspace message, whatever its type, after its
 * type's own handler (replaces any prior observer). */
export function setEmbedderMessageObserver(observer: EmbedderMessageHandler): void {
  getEndpoint();
  messageObserver = observer;
}

/** Tell the embedder this page is listening, so it can send what it held.
 * Called once per load, after the handlers that the held messages need. */
export function announceReadyToEmbedder(): void {
  sendToEmbedder(WORKSPACE_READY);
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
  messageObserver = null;
}
