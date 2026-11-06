/**
 * Mentions as the page reads them (the multiplayer spec, sections 7.1, 7.2, and 7.4): the handle table the
 * composer's popover offers (``GET /api/mentions``), and the chats the viewer has an unseen mention in
 * (``GET /api/mentions/pending``), which the chat list marks with an ``@`` until they have looked.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { addMentionsUpdatedListener } from "./Chats";

/** One handle and whom it names: a participant (by key) or a chat (by id). */
export interface MentionHandle {
  handle: string;
  display_name: string;
  /** The roster's participant kind, or "chat". */
  kind: string;
  key: string | null;
  chat_id: string | null;
}

interface PendingMention {
  chat_id: string;
  message_id: string;
  sender: string;
}

let handles: MentionHandle[] = [];
let handlesLoadedAt = 0;
let handlesLoad: Promise<void> | null = null;
let pendingChatIds: ReadonlySet<string> = new Set();
let pendingLoad: Promise<void> | null = null;
let isListening = false;

/** How long a handle table serves the popover before it is read again: a rename or a new room reaches it within this. */
export const HANDLES_FRESH_MS = 15_000;

export function getMentionHandles(): MentionHandle[] {
  return handles;
}

/** The chats the viewer has an unseen mention in. */
export function getMentionedChatIds(): ReadonlySet<string> {
  return pendingChatIds;
}

/** Read the handle table unless a fresh one is at hand; a failed read keeps the last table. */
export function ensureMentionHandles(): Promise<void> {
  if (handlesLoad !== null) return handlesLoad;
  if (Date.now() - handlesLoadedAt < HANDLES_FRESH_MS) return Promise.resolve();
  handlesLoad = m
    .request<{ handles: MentionHandle[] }>({ method: "GET", url: apiUrl("/api/mentions") })
    .then((table) => {
      handles = table.handles;
      handlesLoadedAt = Date.now();
    })
    .catch((error: unknown) => {
      console.warn("Failed to load the mention handles", error);
    })
    .finally(() => {
      handlesLoad = null;
    });
  return handlesLoad;
}

/** Read the viewer's pending mentions, and again on every ``mentions_updated`` push. */
export function watchPendingMentions(): Promise<void> {
  if (!isListening) {
    isListening = true;
    addMentionsUpdatedListener(() => void loadPendingMentions());
  }
  return loadPendingMentions();
}

function loadPendingMentions(): Promise<void> {
  if (pendingLoad !== null) return pendingLoad;
  pendingLoad = m
    .request<{ mentions: PendingMention[] }>({ method: "GET", url: apiUrl("/api/mentions/pending") })
    .then((answer) => {
      pendingChatIds = new Set(answer.mentions.map((mention) => mention.chat_id));
      m.redraw();
    })
    .catch((error: unknown) => {
      console.warn("Failed to load the pending mentions", error);
    })
    .finally(() => {
      pendingLoad = null;
    });
  return pendingLoad;
}

/** The handles the popover lists for what was typed after the ``@``: those starting with it first, then those
 *  containing it, people first, then agents, then chats (the table's own order). */
export function matchingHandles(query: string, table: readonly MentionHandle[] = handles): MentionHandle[] {
  const needle = query.toLowerCase();
  const starts = table.filter((entry) => entry.handle.toLowerCase().startsWith(needle));
  const contains = table.filter(
    (entry) => !starts.includes(entry) && (entry.handle + " " + entry.display_name).toLowerCase().includes(needle),
  );
  return [...starts, ...contains];
}

/** Make the handle table and the pending set given ones, without a request. Test-only. */
export function setMentionsForTesting(table: MentionHandle[], mentioned: readonly string[] = []): void {
  handles = table;
  handlesLoadedAt = Date.now();
  pendingChatIds = new Set(mentioned);
}
