/**
 * A setting that belongs to one chat and lives on the backend beside the chat's record, at
 * ``/api/chats/<id>/<endpoint>`` (GET answers ``{state}``, PUT takes the whole state and answers
 * ``{state}``). The page keeps one copy per chat, loaded on demand and replaced whole by every
 * write.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export const RETRY_DELAY_MS = 30_000;

export interface ChatSettingStoreOptions {
  // The last path segment under ``/api/chats/<id>/``, e.g. ``"fast-mode"``.
  endpoint: string;
  // What the setting is called in a warning, e.g. ``"fast mode"``.
  description: string;
}

export interface ChatSettingStore<T extends object> {
  /** The chat's setting as this page last saw it, or null before the first load answered. */
  get: (chatId: string) => T | null;
  /**
   * Load the chat's setting once and redraw when it lands; later calls share the first load. A
   * load that fails is warned about and resolves null, leaving nothing loaded until
   * ``RETRY_DELAY_MS`` has passed.
   */
  ensure: (chatId: string) => Promise<T | null>;
  /**
   * Replace the chat's setting, on the page at once and on the backend; the backend's answer is
   * what stays. A write the backend refuses or never receives puts the previous state back.
   */
  set: (chatId: string, next: T) => Promise<T | null>;
}

export function createChatSettingStore<T extends object>({
  endpoint,
  description,
}: ChatSettingStoreOptions): ChatSettingStore<T> {
  const path = `/api/chats/:chatId/${endpoint}`;
  const stateByChat = new Map<string, T>();
  const loadingByChat = new Map<string, Promise<T | null>>();
  // After a failed load, when the backend may be asked again for that chat; the callers ask on
  // every render, so a failure has to hold them off rather than be retried by the next redraw.
  const retryNotBeforeByChat = new Map<string, number>();

  function get(chatId: string): T | null {
    return stateByChat.get(chatId) ?? null;
  }

  function ensure(chatId: string): Promise<T | null> {
    const known = stateByChat.get(chatId);
    if (known !== undefined) return Promise.resolve(known);
    const loading = loadingByChat.get(chatId);
    if (loading !== undefined) return loading;
    if (Date.now() < (retryNotBeforeByChat.get(chatId) ?? 0)) return Promise.resolve(null);
    const request = m
      .request<{ state: T }>({ method: "GET", url: apiUrl(path), params: { chatId } })
      .then((response) => {
        stateByChat.set(chatId, response.state);
        m.redraw();
        return response.state;
      })
      .catch((error: unknown) => {
        console.warn(`Failed to load the ${description} of chat ${chatId}`, error);
        retryNotBeforeByChat.set(chatId, Date.now() + RETRY_DELAY_MS);
        return null;
      })
      .finally(() => {
        loadingByChat.delete(chatId);
      });
    loadingByChat.set(chatId, request);
    return request;
  }

  async function set(chatId: string, next: T): Promise<T | null> {
    const previous = stateByChat.get(chatId);
    stateByChat.set(chatId, next);
    m.redraw();
    try {
      const response = await m.request<{ state: T }>({
        method: "PUT",
        url: apiUrl(path),
        params: { chatId },
        body: next,
      });
      stateByChat.set(chatId, response.state);
      return response.state;
    } catch (error) {
      console.warn(`Failed to save the ${description} of chat ${chatId}`, error);
      if (previous === undefined) stateByChat.delete(chatId);
      else stateByChat.set(chatId, previous);
      return previous ?? null;
    } finally {
      m.redraw();
    }
  }

  return { get, ensure, set };
}
