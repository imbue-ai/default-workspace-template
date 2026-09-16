/**
 * The workspace-wide chat settings (the backend's ``ChatSettings``, at ``/api/settings``): how many
 * of the user's turns a new chat runs fast for, and whether the user has been told about the
 * first automatic switch to standard speed. One copy per page, loaded on demand and replaced
 * whole by every write.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface ChatSettings {
  // User turns a new chat runs fast for before fast mode is turned off; 0 launches every chat
  // at standard speed.
  fast_mode_turn_limit: number;
  is_fast_mode_notice_shown: boolean;
}

/** The backend's defaults, so a page that has not loaded yet behaves as a fresh workspace would. */
export const DEFAULT_CHAT_SETTINGS: ChatSettings = { fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false };

let settings: ChatSettings | null = null;
let loading: Promise<ChatSettings> | null = null;

/** The settings as this page last saw them, or null before the first load answered. */
export function getChatSettings(): ChatSettings | null {
  return settings;
}

/** Load the settings once; later calls share the first load. */
export function ensureChatSettings(): Promise<ChatSettings> {
  if (settings !== null) return Promise.resolve(settings);
  if (loading === null) {
    loading = m
      .request<{ settings: ChatSettings }>({ method: "GET", url: apiUrl("/api/settings") })
      .then((response) => {
        settings = response.settings;
        return settings;
      })
      .finally(() => {
        loading = null;
      });
  }
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
}
