/**
 * What every chat is doing (the multiplayer spec, section 6.4), as the chat app reports it to the shell and the
 * shell pushes it (`chats_activity`, on connect once reported and on every chat-list change): what the agent's
 * card lists under "Working on" and "Needs you".
 */

import type { ChatActivity } from "./records";

let chats: ChatActivity[] = [];

export function getChatsActivity(): ChatActivity[] {
  return chats;
}

export function applyChatsActivity(next: ChatActivity[]): void {
  chats = next;
}

/** The chats the agent is working on: a turn in flight, or a background task about to start one. */
export function workingOn(): ChatActivity[] {
  return chats.filter((chat) => chat.status === "working" || chat.status === "background");
}

/** The chats waiting on the user: a permission request stands. */
export function needsYou(): ChatActivity[] {
  return chats.filter((chat) => chat.status === "attention");
}

/** Forget every report. Test-only. */
export function resetChatsActivityForTesting(): void {
  chats = [];
}
