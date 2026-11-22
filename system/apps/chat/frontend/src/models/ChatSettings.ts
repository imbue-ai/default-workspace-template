/**
 * The workspace-wide chat settings (the backend's ``ChatSettings``, at ``/api/settings``). One copy
 * per page, loaded on demand, replaced whole by what every write answers, and read again when the chat app says
 * they changed.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import type { FastModeMode } from "./FastMode";

/** The switch of each suggestion placement (``models/Suggestions.ts``), workspace-wide. */
export interface SuggestionSwitches {
  is_next_step_suggestions_enabled: boolean;
  is_activity_suggestions_enabled: boolean;
  is_new_chat_suggestions_enabled: boolean;
}

export type SuggestionSwitch = keyof SuggestionSwitches;

// The suggestion switches are absent from an older backend's answer, where they read as on.
export interface ChatSettings extends Partial<SuggestionSwitches> {
  // The fast mode a new chat starts in.
  fast_mode_default: FastModeMode;
  // User turns a chat in auto mode runs fast for before it is switched to standard speed.
  fast_mode_turn_limit: number;
  is_fast_mode_notice_shown: boolean;
  // Whether a new chat starts with auto-compact on.
  autocompact_default: boolean;
  is_autocompact_notice_shown: boolean;
  // Whether the one-time hint at the send button, shown on the first pick from a first-run picker, has been shown.
  // Absent from an older backend's answer, where it reads as not shown.
  is_picker_send_hint_shown?: boolean;
}

/** The backend's defaults, so a page that has not loaded yet behaves as a fresh workspace would. */
export const DEFAULT_CHAT_SETTINGS: ChatSettings = {
  fast_mode_default: "off",
  fast_mode_turn_limit: 2,
  is_fast_mode_notice_shown: false,
  autocompact_default: true,
  is_autocompact_notice_shown: false,
};

let settings: ChatSettings | null = null;
let loading: Promise<ChatSettings> | null = null;
// After a failed load, when the backend may be asked again. The callers ask on every render,
// so a failure has to hold them off rather than be retried by the next redraw.
let retryNotBefore = 0;
export const RETRY_DELAY_MS = 30_000;
const settingsUpdatedListeners: (() => void)[] = [];

/** The settings as this page last saw them, or null before the first load answered. */
export function getChatSettings(): ChatSettings | null {
  return settings;
}

/** Whether a suggestion switch is on, or null before the settings have loaded. */
export function isSuggestionSwitchOn(name: SuggestionSwitch): boolean | null {
  return settings === null ? null : settings[name] !== false;
}

/** Notified when the settings were read again because they changed outside this page. */
export function addChatSettingsUpdatedListener(listener: () => void): void {
  settingsUpdatedListeners.push(listener);
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

/**
 * Read the settings again because they changed outside this page (``settings_updated`` on ``/api/ws``), and tell
 * the listeners once they land. A page that holds no copy has nothing to bring up to date: its next load reads
 * them. A read that fails keeps the copy read before.
 */
export function rereadChatSettings(): void {
  if (settings === null) return;
  m.request<{ settings: ChatSettings }>({ method: "GET", url: apiUrl("/api/settings") }).then(
    (response) => {
      settings = response.settings;
      for (const listener of settingsUpdatedListeners) listener();
      m.redraw();
    },
    (error: unknown) => console.warn("Failed to re-read the chat settings", error),
  );
}

/**
 * Change the suggestion switches named, and only those, on the page at once and on the backend
 * (``PUT /api/settings/suggestions``); the backend's answer is what stays. A write the backend refuses or never
 * receives puts the previous settings back.
 */
export async function updateSuggestionSwitches(switches: Partial<SuggestionSwitches>): Promise<void> {
  const previous = settings;
  if (previous !== null) settings = { ...previous, ...switches };
  m.redraw();
  try {
    const response = await m.request<{ settings: ChatSettings }>({
      method: "PUT",
      url: apiUrl("/api/settings/suggestions"),
      body: switches,
    });
    settings = response.settings;
  } catch (error) {
    console.warn("Failed to save the suggestion settings", error);
    settings = previous;
  }
  m.redraw();
}

/** Forget what was loaded, so the next read fetches afresh (tests). */
export function resetChatSettingsForTests(): void {
  settings = null;
  loading = null;
  retryNotBefore = 0;
}
