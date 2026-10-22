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
   * what stays. A write the backend refuses or never receives puts back the last state the backend
   * confirmed, and resolves with it. Only the chat's latest write settles what the page holds: an
   * earlier one answering late changes nothing, and neither does a load that answers after a write.
   */
  set: (chatId: string, next: T) => Promise<T | null>;
  /**
   * Drop every chat's loaded setting so the next read loads it afresh, for after a change that can
   * alter what the backend answers for a chat that has made no choice of its own, such as a new
   * workspace default. A load already in flight lands nowhere.
   */
  forgetAll: () => void;
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
  // How many writes each chat has started, so a late answer can tell whether a newer write has
  // replaced what it would set.
  const writeCountByChat = new Map<string, number>();
  // The last state the backend answered for each chat, which a failed write falls back to: the
  // page's own copy may still hold an earlier write's optimistic value.
  const confirmedByChat = new Map<string, T>();
  // Bumped by forgetAll, so a load that started before it does not store a stale answer.
  let generation = 0;

  function writeCount(chatId: string): number {
    return writeCountByChat.get(chatId) ?? 0;
  }

  function get(chatId: string): T | null {
    return stateByChat.get(chatId) ?? null;
  }

  function ensure(chatId: string): Promise<T | null> {
    const known = stateByChat.get(chatId);
    if (known !== undefined) return Promise.resolve(known);
    const loading = loadingByChat.get(chatId);
    if (loading !== undefined) return loading;
    if (Date.now() < (retryNotBeforeByChat.get(chatId) ?? 0)) return Promise.resolve(null);
    const writeCountAtLoad = writeCount(chatId);
    const generationAtLoad = generation;
    const request: Promise<T | null> = m
      .request<{ state: T }>({ method: "GET", url: apiUrl(path), params: { chatId } })
      .then((response) => {
        if (generation !== generationAtLoad || writeCount(chatId) !== writeCountAtLoad) return get(chatId);
        confirmedByChat.set(chatId, response.state);
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
        if (loadingByChat.get(chatId) === request) loadingByChat.delete(chatId);
      });
    loadingByChat.set(chatId, request);
    return request;
  }

  async function set(chatId: string, next: T): Promise<T | null> {
    const thisWrite = writeCount(chatId) + 1;
    writeCountByChat.set(chatId, thisWrite);
    stateByChat.set(chatId, next);
    m.redraw();
    try {
      const response = await m.request<{ state: T }>({
        method: "PUT",
        url: apiUrl(path),
        params: { chatId },
        body: next,
      });
      confirmedByChat.set(chatId, response.state);
      if (writeCount(chatId) === thisWrite) stateByChat.set(chatId, response.state);
      return response.state;
    } catch (error) {
      console.warn(`Failed to save the ${description} of chat ${chatId}`, error);
      const confirmed = confirmedByChat.get(chatId);
      if (writeCount(chatId) === thisWrite) {
        if (confirmed === undefined) stateByChat.delete(chatId);
        else stateByChat.set(chatId, confirmed);
      }
      return confirmed ?? null;
    } finally {
      m.redraw();
    }
  }

  function forgetAll(): void {
    generation += 1;
    stateByChat.clear();
    confirmedByChat.clear();
    loadingByChat.clear();
    retryNotBeforeByChat.clear();
    m.redraw();
  }

  return { get, ensure, set, forgetAll };
}
