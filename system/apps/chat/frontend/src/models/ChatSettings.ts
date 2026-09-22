/**
 * The workspace-wide chat settings (the backend's ``ChatSettings``, at ``/api/settings``): the fast
 * mode a new chat starts in, how many of the user's turns a chat in auto mode runs fast for, and
 * whether the user has been told about the first automatic switch to standard speed. One copy
 * per page, loaded on demand and replaced whole by every write.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { FastModeMode } from "./FastMode";

export interface ChatSettings {
  // The fast mode a new chat starts in.
  fast_mode_default: FastModeMode;
  // User turns a chat in auto mode runs fast for before it is switched to standard speed.
  fast_mode_turn_limit: number;
  is_fast_mode_notice_shown: boolean;
}

/** The backend's defaults, so a page that has not loaded yet behaves as a fresh workspace would. */
export const DEFAULT_CHAT_SETTINGS: ChatSettings = {
  fast_mode_default: "auto",
  fast_mode_turn_limit: 5,
  is_fast_mode_notice_shown: false,
};

let settings: ChatSettings | null = null;
let loading: Promise<ChatSettings> | null = null;
// After a failed load, when the backend may be asked again. The callers ask on every render,
// so a failure has to hold them off rather than be retried by the next redraw.
let retryNotBefore = 0;
export const RETRY_DELAY_MS = 30_000;

/** The settings as this page last saw them, or null before the first load answered. */
export function getChatSettings(): ChatSettings | null {
  return settings;
}

/**
 * Load the settings once and redraw when they land; later calls share the first load. A load
 * that fails is warned about and resolves with the defaults, leaving nothing loaded: a call
 * after ``RETRY_DELAY_MS`` asks again, and one before it gets the defaults without a request,
 * so callers that ask on every render cannot turn a backend outage into a request per frame.
 */
export function ensureChatSettings(): Promise<ChatSettings> {
  if (settings !== null) return Promise.resolve(settings);
  if (loading !== null) return loading;
  if (Date.now() < retryNotBefore) return Promise.resolve(DEFAULT_CHAT_SETTINGS);
  loading = m
    .request<{ settings: ChatSettings }>({ method: "GET", url: apiUrl("/api/settings") })
    .then((response) => {
      settings = response.settings;
      m.redraw();
      return settings;
    })
    .catch((error: unknown) => {
      console.warn("Failed to load the chat settings", error);
      retryNotBefore = Date.now() + RETRY_DELAY_MS;
      return DEFAULT_CHAT_SETTINGS;
    })
    .finally(() => {
      loading = null;
    });
  return loading;
}

/**
 * Replace the settings, on the page at once and on the backend; the backend's answer is what
 * stays. A write the backend refuses or never receives puts the previous settings back, so the
 * page never shows a limit that does not apply to the next chat, and resolves with them.
 */
export async function updateChatSettings(next: ChatSettings): Promise<ChatSettings> {
  const previous = settings;
  settings = next;
  m.redraw();
  try {
    const response = await m.request<{ settings: ChatSettings }>({
      method: "PUT",
      url: apiUrl("/api/settings"),
      body: next,
    });
    settings = response.settings;
  } catch (error) {
    console.warn("Failed to save the chat settings", error);
    settings = previous;
  }
  m.redraw();
  return settings ?? DEFAULT_CHAT_SETTINGS;
}

/** Forget what was loaded, so the next read fetches afresh (tests). */
export function resetChatSettingsForTests(): void {
  settings = null;
  loading = null;
  retryNotBefore = 0;
}
