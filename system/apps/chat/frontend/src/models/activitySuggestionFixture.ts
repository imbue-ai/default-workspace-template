/** The four activity suggestions for tests, as the chat app answers them (``GET /api/suggestions/activity``). */

import type { ActivityAnswer } from "./ActivitySuggestions";

export const NEW_CHAT = {
  suggestion_id: "new_chat",
  label: "Work in parallel by starting a new chat",
  action: "new_chat_coach",
};
export const PHONE = { suggestion_id: "phone_access", label: "Access Studio from your phone", action: "phone_access" };
export const BUILD_APP = { suggestion_id: "build_app", label: "Build an app", action: "build_app_coach" };
export const CONNECT_DATA = {
  suggestion_id: "connect_data",
  label: "Connect some data",
  action: "connect_data_coach",
};

/** What the chat app answers a workspace whose owner has done none of it: all four, in their fixed order. */
export const ALL_FOUR: ActivityAnswer = { suggestions: [NEW_CHAT, PHONE, BUILD_APP, CONNECT_DATA] };
