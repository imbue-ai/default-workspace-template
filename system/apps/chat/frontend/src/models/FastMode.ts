/**
 * A chat's fast mode (the backend's ``ChatFastModeState``, at ``/api/chats/<id>/fast-mode``):
 * off, auto or on, and for auto whether the chat has run its fast turns and been switched to
 * standard speed. The choice belongs to the chat, so it lives on the backend beside the chat's
 * record and this page keeps one copy per chat, loaded on demand and replaced whole by every
 * write.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export type FastModeMode = "off" | "auto" | "on";

export interface ChatFastModeState {
  mode: FastModeMode;
  // Auto only: the chat has run its fast turns and was switched to standard speed.
  is_switched: boolean;
}

const stateByChat = new Map<string, ChatFastModeState>();
const loadingByChat = new Map<string, Promise<ChatFastModeState | null>>();
// After a failed load, when the backend may be asked again for that chat; the callers ask on
// every render, so a failure has to hold them off rather than be retried by the next redraw.
const retryNotBeforeByChat = new Map<string, number>();
export const RETRY_DELAY_MS = 30_000;

/** The chat's fast mode as this page last saw it, or null before the first load answered. */
export function getFastModeState(chatId: string): ChatFastModeState | null {
  return stateByChat.get(chatId) ?? null;
}

/**
 * Load the chat's fast mode once and redraw when it lands; later calls share the first load. A
 * load that fails is warned about and resolves null, leaving nothing loaded until
 * ``RETRY_DELAY_MS`` has passed.
 */
export function ensureFastModeState(chatId: string): Promise<ChatFastModeState | null> {
  const known = stateByChat.get(chatId);
  if (known !== undefined) return Promise.resolve(known);
  const loading = loadingByChat.get(chatId);
  if (loading !== undefined) return loading;
  if (Date.now() < (retryNotBeforeByChat.get(chatId) ?? 0)) return Promise.resolve(null);
  const request = m
    .request<{ state: ChatFastModeState }>({
      method: "GET",
      url: apiUrl("/api/chats/:chatId/fast-mode"),
      params: { chatId },
    })
    .then((response) => {
      stateByChat.set(chatId, response.state);
      m.redraw();
      return response.state;
    })
    .catch((error: unknown) => {
      console.warn(`Failed to load the fast mode of chat ${chatId}`, error);
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
 * Replace the chat's fast mode, on the page at once and on the backend; the backend's answer is
 * what stays. A write the backend refuses or never receives puts the previous state back.
 */
export async function updateFastModeState(chatId: string, next: ChatFastModeState): Promise<ChatFastModeState | null> {
  const previous = stateByChat.get(chatId);
  stateByChat.set(chatId, next);
  m.redraw();
  try {
    const response = await m.request<{ state: ChatFastModeState }>({
      method: "PUT",
      url: apiUrl("/api/chats/:chatId/fast-mode"),
      params: { chatId },
      body: next,
    });
    stateByChat.set(chatId, response.state);
    return response.state;
  } catch (error) {
    console.warn(`Failed to save the fast mode of chat ${chatId}`, error);
    if (previous === undefined) stateByChat.delete(chatId);
    else stateByChat.set(chatId, previous);
    return previous ?? null;
  } finally {
    m.redraw();
  }
}

/** What each mode is called, everywhere one is named. Keyed by the mode rather than searched
 *  for in a list, so every mode has a name by construction and no caller needs a fallback.
 *  Written in the order the chooser offers the modes in, which `FAST_MODES` reads off it. */
export const FAST_MODE_LABELS: Readonly<Record<FastModeMode, string>> = {
  off: "Off",
  auto: "Auto",
  on: "On",
};

/** The modes, in the order the chooser offers them. Taken from the table above rather than
 *  written out again: that table is keyed by the mode, so every mode is in it by construction,
 *  where a second list of names is the one spelling of the set nothing checks -- a mode added to
 *  `FastModeMode` would compile while the chooser quietly stopped offering it. */
export const FAST_MODES: readonly FastModeMode[] = Object.keys(FAST_MODE_LABELS) as FastModeMode[];

/** What the model picker's fast row says for a state: the mode's name, and for auto whether it
 *  has already run its fast turns. */
export function fastModeLabel(state: ChatFastModeState): string {
  const label = FAST_MODE_LABELS[state.mode];
  return state.mode === "auto" && state.is_switched ? `${label} (off now)` : label;
}

/** The line under a mode in the chooser; auto's names the limit it runs to. */
export function fastModeDetail(mode: FastModeMode, turnLimit: number): string {
  if (mode === "off") return "Standard speed always";
  if (mode === "on") return "Fast mode always";
  const turns = turnLimit === 1 ? "1 turn" : `${turnLimit} turns`;
  return `Fast for the first ${turns}, then standard`;
}

/** Forget every chat's state, so a test starts clean. */
export function resetFastModeForTests(): void {
  stateByChat.clear();
  loadingByChat.clear();
  retryNotBeforeByChat.clear();
}
