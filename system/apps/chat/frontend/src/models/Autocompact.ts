/**
 * A chat's auto-compact setting (the backend's ``ChatAutocompactState``, at
 * ``/api/chats/<id>/autocompact``): whether the chat is compacted while idle, just before its
 * prompt cache would expire. The choice belongs to the chat, so it lives on the backend beside the
 * chat's record and this page keeps one copy per chat, loaded on demand and replaced whole by
 * every write.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface ChatAutocompactState {
  is_enabled: boolean;
}

const stateByChat = new Map<string, ChatAutocompactState>();
const loadingByChat = new Map<string, Promise<ChatAutocompactState | null>>();
// After a failed load, when the backend may be asked again for that chat; the callers ask on
// every render, so a failure has to hold them off rather than be retried by the next redraw.
const retryNotBeforeByChat = new Map<string, number>();
export const RETRY_DELAY_MS = 30_000;

/** The chat's auto-compact setting as this page last saw it, or null before the first load answered. */
export function getAutocompactState(chatId: string): ChatAutocompactState | null {
  return stateByChat.get(chatId) ?? null;
}

/**
 * Load the chat's auto-compact setting once and redraw when it lands; later calls share the first
 * load. A load that fails is warned about and resolves null, leaving nothing loaded until
 * ``RETRY_DELAY_MS`` has passed.
 */
export function ensureAutocompactState(chatId: string): Promise<ChatAutocompactState | null> {
  const known = stateByChat.get(chatId);
  if (known !== undefined) return Promise.resolve(known);
  const loading = loadingByChat.get(chatId);
  if (loading !== undefined) return loading;
  if (Date.now() < (retryNotBeforeByChat.get(chatId) ?? 0)) return Promise.resolve(null);
  const request = m
    .request<{ state: ChatAutocompactState }>({
      method: "GET",
      url: apiUrl("/api/chats/:chatId/autocompact"),
      params: { chatId },
    })
    .then((response) => {
      stateByChat.set(chatId, response.state);
      m.redraw();
      return response.state;
    })
    .catch((error: unknown) => {
      console.warn(`Failed to load the auto-compact setting of chat ${chatId}`, error);
      retryNotBeforeByChat.set(chatId, Date.now() + RETRY_DELAY_MS);
      return null;
    })
    .finally(() => {
      loadingByChat.delete(chatId);
    });
  loadingByChat.set(chatId, request);
  return request;
}

/**
 * Replace the chat's auto-compact setting, on the page at once and on the backend; the backend's
 * answer is what stays. A write the backend refuses or never receives puts the previous state back.
 */
export async function setAutocompactState(
  chatId: string,
  next: ChatAutocompactState,
): Promise<ChatAutocompactState | null> {
  const previous = stateByChat.get(chatId);
  stateByChat.set(chatId, next);
  m.redraw();
  try {
    const response = await m.request<{ state: ChatAutocompactState }>({
      method: "PUT",
      url: apiUrl("/api/chats/:chatId/autocompact"),
      params: { chatId },
      body: next,
    });
    stateByChat.set(chatId, response.state);
    return response.state;
  } catch (error) {
    console.warn(`Failed to save the auto-compact setting of chat ${chatId}`, error);
    if (previous === undefined) stateByChat.delete(chatId);
    else stateByChat.set(chatId, previous);
    return previous ?? null;
  } finally {
    m.redraw();
  }
}

/** What the setting is called in each position, everywhere one is named. */
export function autocompactLabel(isEnabled: boolean): string {
  return isEnabled ? "On" : "Off";
}

/** Forget every chat's state, so a test starts clean. */
export function resetAutocompactForTests(): void {
  stateByChat.clear();
  loadingByChat.clear();
  retryNotBeforeByChat.clear();
}
