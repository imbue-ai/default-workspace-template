/**
 * The shell's child-frame boundary: every message that crosses between the shell document
 * and the frames it created goes through here (workspace app model, contracts.md sections 10
 * and 11). One of the sanctioned postMessage modules the embed ratchet allows.
 *
 * Two jobs, both over one `message` listener:
 *
 * - The embedder relay. The minds chrome accepts messages only from its direct child, the
 *   shell, so an app page (a chat, from phase 6 on) cannot reach it. Any `minds:` message
 *   from a child frame in the workspace origin family is forwarded up to `window.parent`
 *   unchanged, and any message from `window.parent` is rebroadcast to every child frame the
 *   shell created. No payload is inspected.
 * - The shell side of the app contract. `shell:` messages from a child frame are dispatched
 *   to the handler registered for their type, with the frame that posted them; the shell's
 *   own messages to a frame (`shell:handshake`, `shell:shown`, ...) are sent through
 *   `sendToChildFrame`.
 *
 * Trust: a child frame's message counts only when `event.source` is the `contentWindow` of an
 * iframe in this document and `event.origin` shares this shell's workspace coordinate (the
 * origin family the minds chrome checks). The chrome's messages count only from
 * `window.parent`, and only when this shell is framed at all.
 */

import { workspaceHostCoordinate } from "./origin";

export type ChildFrameMessageHandler = (frame: HTMLIFrameElement, payload: Record<string, unknown>) => void;

const MINDS_TYPE_PREFIX = "minds:";
const SHELL_TYPE_PREFIX = "shell:";

const handlerByType: Partial<Record<string, ChildFrameMessageHandler>> = {};
let isInitialized = false;

/** Whether an origin belongs to this workspace's origin family: the same coordinate as the shell's own host. */
export function isWorkspaceFamilyOrigin(origin: string, shellHost: string = window.location.host): boolean {
  let host: string;
  try {
    host = new URL(origin).host;
  } catch {
    return false;
  }
  if (host === "") return false;
  return workspaceHostCoordinate(host) === workspaceHostCoordinate(shellHost);
}

function childFrames(): HTMLIFrameElement[] {
  return Array.from(document.querySelectorAll<HTMLIFrameElement>("iframe"));
}

function frameForSource(source: MessageEventSource | null): HTMLIFrameElement | null {
  if (source === null) return null;
  return childFrames().find((frame) => frame.contentWindow === source) ?? null;
}

function handleMessage(event: MessageEvent): void {
  const data: unknown = event.data;
  if (data === null || typeof data !== "object") return;
  const message = data as Record<string, unknown>;
  const type = message.type;
  if (typeof type !== "string") return;

  // Downward: whatever the chrome sends, every frame the shell created gets, unchanged.
  if (window.parent !== window && event.source === window.parent) {
    for (const frame of childFrames()) {
      frame.contentWindow?.postMessage(data, "*");
    }
    return;
  }

  const frame = frameForSource(event.source);
  if (frame === null || !isWorkspaceFamilyOrigin(event.origin)) return;
  if (type.startsWith(MINDS_TYPE_PREFIX)) {
    // Upward: unchanged, and only when there is a chrome to forward to.
    if (window.parent !== window) window.parent.postMessage(data, "*");
    return;
  }
  if (type.startsWith(SHELL_TYPE_PREFIX)) {
    handlerByType[type]?.(frame, message);
  }
}

/** Start the boundary. Called once, when the shell boots; safe to call again. */
export function initEmbedderRelay(): void {
  if (isInitialized) return;
  isInitialized = true;
  window.addEventListener("message", handleMessage);
}

/** Register the handler for one `shell:` type posted by a child frame (replaces any prior one). */
export function setChildFrameMessageHandler(type: string, handler: ChildFrameMessageHandler): void {
  handlerByType[type] = handler;
}

/** Send one contract message to a frame the shell created. */
export function sendToChildFrame(frame: HTMLIFrameElement, type: string, payload: Record<string, unknown> = {}): void {
  frame.contentWindow?.postMessage({ type, ...payload }, "*");
}

/** Tear the boundary down so the next init rebinds to the current `window`. Test-only. */
export function resetEmbedderRelayForTesting(): void {
  if (isInitialized) {
    window.removeEventListener("message", handleMessage);
    isInitialized = false;
  }
  for (const type of Object.keys(handlerByType)) delete handlerByType[type];
}
