/**
 * A chat's auto-compact setting (the backend's ``ChatAutocompactState``, at
 * ``/api/chats/<id>/autocompact``): whether the chat is compacted while idle, just before its
 * prompt cache would expire. The choice belongs to the chat, so it lives on the backend beside the
 * chat's record.
 */

import { createChatSettingStore } from "./chatSettingStore";

export interface ChatAutocompactState {
  is_enabled: boolean;
}

export const {
  get: getAutocompactState,
  ensure: ensureAutocompactState,
  set: setAutocompactState,
} = createChatSettingStore<ChatAutocompactState>({ endpoint: "autocompact", description: "auto-compact setting" });

/** What the setting is called in each position, everywhere one is named. */
export function autocompactLabel(isEnabled: boolean): string {
  return isEnabled ? "On" : "Off";
}
