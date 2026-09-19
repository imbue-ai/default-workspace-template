/**
 * The rows of the chat root's list, and their order: pure functions over the chat snapshots
 * and provisional chats the chat app streams, so the list is testable without a DOM.
 *
 * The list is ordered by recency: the chat most recently messaged leads, one never messaged
 * trails, except one started from this root, which leads until its first message. A chat
 * another agent started (a worker, labelled ``agent_created`` and ``lead_agent`` by
 * ``create_worker.py``) is filed under the chat that owns the agent the lead label names, one
 * level deep, and the pair moves together.
 */

import type { ChatSnapshot, ProvisionalChat, ProvisionalChatPhase } from "../models/Chats";

export interface ChatRow {
  chatId: string;
  title: string;
  /** The contract's status (``working``, ``idle``, ``attention``, ``stopped``, ``error``). */
  status: string;
  labels: Readonly<Record<string, string>>;
  /** Every agent of the chat, in order; a chat that moved to a new agent keeps its old ones. */
  agentIds: readonly string[];
  lastActiveMs: number | null;
  /** True for a chat the app has minted but that is not an agent yet. */
  isProvisional: boolean;
}

// What a chat that is not an agent yet is doing, in the contract's terms (the backend's
// ``_STATUS_BY_PROVISIONAL_PHASE``).
const STATUS_BY_PROVISIONAL_PHASE: Readonly<Record<ProvisionalChatPhase, string>> = {
  awaiting_account: "attention",
  awaiting_first_send: "idle",
  creating: "working",
  failed: "error",
};

const PROVISIONAL_TITLE = "New chat";

export function rowsFromSnapshots(chats: readonly ChatSnapshot[], provisional: readonly ProvisionalChat[]): ChatRow[] {
  const known = new Set(chats.map((chat) => chat.chat_id));
  const rows: ChatRow[] = chats.map((chat) => ({
    chatId: chat.chat_id,
    title: chat.title,
    status: chat.status,
    labels: chat.labels,
    agentIds: chat.agent_ids,
    lastActiveMs: chat.last_messaged_at === null ? null : chat.last_messaged_at * 1000,
    isProvisional: false,
  }));
  for (const chat of provisional) {
    if (known.has(chat.chat_id)) continue;
    rows.push({
      chatId: chat.chat_id,
      title: chat.name === "" ? PROVISIONAL_TITLE : chat.name,
      status: STATUS_BY_PROVISIONAL_PHASE[chat.phase],
      labels: {},
      agentIds: [],
      lastActiveMs: null,
      isProvisional: true,
    });
  }
  return rows;
}

export function isAgentStarted(row: ChatRow): boolean {
  return row.labels.agent_created === "true";
}

function recencyKey(row: ChatRow, startedHere: ReadonlySet<string>): number {
  if (row.lastActiveMs !== null) return row.lastActiveMs;
  return startedHere.has(row.chatId) ? Number.POSITIVE_INFINITY : 0;
}

function byRecency(rows: ChatRow[], startedHere: ReadonlySet<string>): ChatRow[] {
  return [...rows].sort((first, second) => recencyKey(second, startedHere) - recencyKey(first, startedHere));
}

function ownsAgent(row: ChatRow, agentId: string): boolean {
  return row.chatId === agentId || row.agentIds.includes(agentId);
}

/** The row that started ``row``, when listed: the one owning the agent the ``lead_agent`` label
 *  names. A helper of a helper is filed under the root of that chain, so the list is one level deep. */
export function parentOf(row: ChatRow, rows: readonly ChatRow[]): ChatRow | null {
  let current = row;
  let parent: ChatRow | null = null;
  for (let depth = 0; depth < rows.length; depth += 1) {
    const leadAgent = current.labels.lead_agent;
    if (!isAgentStarted(current) || leadAgent === undefined) break;
    const next = rows.find((candidate) => candidate.chatId !== current.chatId && ownsAgent(candidate, leadAgent));
    if (next === null || next === undefined) break;
    parent = next;
    current = next;
  }
  return parent;
}

/** The list in display order: each root chat by recency, its helpers right under it. */
export function groupedRows(rows: readonly ChatRow[], startedHere: ReadonlySet<string>): ChatRow[] {
  const helpersByParent = new Map<string, ChatRow[]>();
  const roots: ChatRow[] = [];
  for (const row of rows) {
    const parent = parentOf(row, rows);
    if (parent === null) roots.push(row);
    else helpersByParent.set(parent.chatId, [...(helpersByParent.get(parent.chatId) ?? []), row]);
  }
  const groups = roots.map((root) => ({
    root,
    helpers: byRecency(helpersByParent.get(root.chatId) ?? [], startedHere),
  }));
  const latestOf = (group: { root: ChatRow; helpers: ChatRow[] }): number =>
    Math.max(recencyKey(group.root, startedHere), ...group.helpers.map((helper) => recencyKey(helper, startedHere)));
  groups.sort((first, second) => latestOf(second) - latestOf(first));
  return groups.flatMap((group) => [group.root, ...group.helpers]);
}
