/**
 * The chat pages' live chat state: the chat list (each with its active agent's activity, model
 * choice and queued messages) and the provisional chats (minted here, not agents yet), as the
 * chat app pushes them over its own WebSocket (``/api/ws``).
 */

import m from "mithril";
import { apiUrl, wsUrl } from "@imbue/workspace-ui/src/base-path";
import { getTerminalOriginLabel } from "../document-meta";
import { deriveAppOrigin } from "@imbue/workspace-ui/src/origin";
import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import type { ModelChoice } from "./ModelSettings";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";

/** The agent-level facts about a chat's active agent that the pages render (the backend's
 *  ``ActiveAgentSnapshot``). */
export interface ActiveAgent {
  agent_id: string;
  // The agent's mngr name: what its tmux session is addressed by.
  name: string;
  // The agent's harness ("claude", "codex", ...). Used only as a lookup key into the
  // per-harness catalog (GET /api/harnesses).
  harness: string;
  // The account the agent is bound to (its ``account`` label), or null for one from before accounts.
  account_id: string | null;
  // The agent's mngr lifecycle state.
  state: string;
  // THINKING/TOOL_RUNNING/IDLE, or null when the chat app has no activity tracking for it.
  activity_state: string | null;
  // The live model/effort/fast selection plus the catalog option it matched. Null when no
  // model resolution is available.
  model_choice: ModelChoice | null;
  // Full snapshot of the messages currently parked in the harness queue, in enqueue order.
  // Replaced wholesale on each push; the frontend holds no queued state of its own.
  queued_messages: QueuedMessage[];
  // Backend-computed shoulder-tap availability: true iff something is queued AND no send is in
  // flight.
  shoulder_tap_available: boolean;
}

export type HandoffPhase = "draining" | "summarizing" | "switching" | "failed";

/** The in-progress handoff a chat carries while it converges on a new agent. */
export interface HandoffState {
  phase: HandoffPhase;
  target_lane: string;
  target_account_id: string;
}

/** One chat as the pages see it (the backend's ``ChatSnapshot``, one entry of ``chats_updated``). */
export interface ChatSnapshot {
  chat_id: string;
  // The name the user sees: the ``display_name`` label, else the mngr name.
  title: string;
  // The chat's canonical mngr name, the only way to address it by name.
  name: string;
  // The mngr ``project`` label: the project this chat was created in, which mngr propagates to
  // the agent's own children. Null when the agent carries no label.
  project: string | null;
  // The chat's status, as its instance record reports it.
  status: string;
  // The active agent's mngr labels.
  labels: Record<string, string>;
  // Every agent of the chat, in order; the last is the active one.
  agent_ids: string[];
  // Null while the chat is not converging on a new agent.
  handoff: HandoffState | null;
  active_agent: ActiveAgent;
}

/** One message currently parked in an agent's harness queue (the wire shape of the backend
 *  ``QueuedMessageState``). The frontend renders these verbatim and keys the bubble on
 *  ``queued_id``; it never derives or reconciles them. */
export interface QueuedMessage {
  queued_id: string;
  content: string;
  timestamp: string;
  // True while the backend is actively re-sending this chip (a codex shoulder-tap's
  // interrupt+resend): it renders "Sending…" rather than as a plain queued chip.
  is_sending?: boolean;
}

/** Where a chat that is not an agent yet stands (the backend's ``ProvisionalChatPhase``). */
export type ProvisionalChatPhase = "awaiting_account" | "creating" | "failed";

/** A chat the app minted but mngr does not know yet: the backend's ``ProvisionalChat``. */
export interface ProvisionalChat {
  chat_id: string;
  name: string;
  // The account it launches on; empty while it waits for one.
  account_id: string;
  phase: ProvisionalChatPhase;
  // Why the create failed, in the failed phase.
  error: string | null;
}

type WsEvent =
  | { type: "chats_updated"; chats: ChatSnapshot[] }
  | ({ type: "provisional_chat_created" } & ProvisionalChat)
  | { type: "provisional_chat_completed"; chat_id: string; success: boolean; error: string | null };

export type ChatsUpdatedListener = (chats: ChatSnapshot[]) => void;
/**
 * Notified when a chat's active agent's ``activity_state`` changes between two consecutive
 * ``chats_updated`` snapshots. ``previous`` is ``null`` when the chat had no prior tracked
 * state (it just appeared, or its state was untracked).
 */
export type ChatActivityListener = (chatId: string, previous: string | null, current: string | null) => void;

let chats: ChatSnapshot[] = [];
// The JSON of the last chats_updated payload, to skip redundant identical pushes.
let lastChatsSerialized = "";
let provisionalChats: ProvisionalChat[] = [];
// The ids of the provisional chats a (re)connect's replay has carried so far, while the replay
// is in flight: from the socket opening to the chat list that ends it. Null otherwise.
let replayedProvisionalIds: Set<string> | null = null;
// Who is waiting for a provisional chat to become an agent (a send typed while it was being
// created), settled by the push that registers it or the one that fails it.
const registrationWaiters = new Map<string, { resolve: () => void; reject: (error: Error) => void }[]>();
let chatsUpdatedListeners: ChatsUpdatedListener[] = [];
let chatActivityListeners: ChatActivityListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connected = false;

const reconnectBackoff = new ReconnectBackoff();

function connect(): void {
  if (ws !== null) return;
  const url = wsUrl("/api/ws");
  console.info(`[chat-ws] connecting to ${url}`);
  ws = new WebSocket(url);

  ws.onopen = () => {
    connected = true;
    console.info("[chat-ws] connected");
    reconnectBackoff.reset();
    // The app replays what it holds (its provisional chats, then its chat list) on every
    // connection; the list ends the replay and must be handled even when nothing changed.
    replayedProvisionalIds = new Set();
    lastChatsSerialized = "";
    m.redraw();
  };

  ws.onmessage = (event: MessageEvent) => {
    const data = parseJsonMessage<WsEvent>(event.data as string);
    if (data === null) return;
    handleEvent(data);
    m.redraw();
  };

  ws.onclose = (event: CloseEvent) => {
    console.warn(
      `[chat-ws] closed (code=${event.code} reason=${JSON.stringify(event.reason)} wasClean=${event.wasClean})`,
    );
    ws = null;
    connected = false;
    scheduleReconnect();
    m.redraw();
  };

  ws.onerror = () => {
    console.warn("[chat-ws] socket error");
    ws?.close();
  };
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  const delayMs = reconnectBackoff.nextDelay();
  console.info(`[chat-ws] reconnecting in ${delayMs}ms`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delayMs);
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "chats_updated": {
      // The backend can broadcast the same snapshot many times during a turn (transcript
      // churn), and a redraw on each identical push makes the model bar visibly flicker.
      const serialized = JSON.stringify(event.chats);
      if (serialized === lastChatsSerialized) break;
      lastChatsSerialized = serialized;
      // Diff against the outgoing snapshot (still in `chats` here) so per-chat activity
      // transitions can be reported before replacing it.
      const previousActivityById = new Map(chats.map((c) => [c.chat_id, c.active_agent.activity_state]));
      chats = event.chats;
      // A provisional chat the list now names is an agent, whatever order the pushes came in.
      const registeredIds = new Set(chats.map((c) => c.chat_id));
      provisionalChats = provisionalChats.filter((p) => !registeredIds.has(p.chat_id));
      for (const chatId of registeredIds) settleRegistration(chatId, null);
      if (replayedProvisionalIds !== null) {
        // The list ends a (re)connect's replay. A record the app did not replay is one it no
        // longer holds (it restarted while the create ran), so no push is coming for it: the
        // record goes, and a send held for it proceeds to report the backend's refusal.
        const replayed = replayedProvisionalIds;
        replayedProvisionalIds = null;
        provisionalChats = provisionalChats.filter((p) => replayed.has(p.chat_id));
        for (const chatId of [...registrationWaiters.keys()]) {
          if (getProvisionalChat(chatId) === undefined) settleRegistration(chatId, null);
        }
      }
      for (const listener of chatsUpdatedListeners) {
        listener(getChats());
      }
      for (const chat of chats) {
        const current = chat.active_agent.activity_state;
        const previous = previousActivityById.get(chat.chat_id) ?? null;
        if (previous !== current) {
          for (const listener of chatActivityListeners) {
            listener(chat.chat_id, previous, current);
          }
        }
      }
      break;
    }
    case "provisional_chat_created": {
      // Also how a chat moves between phases (a reserved chat launched, a failed one retried):
      // the backend pushes the whole record again. A reconnect replays every provisional chat
      // this way too, so a failed record seen here settles a send held for it as the
      // completion message would have.
      const { type: _type, ...provisional } = event;
      provisionalChats = [...provisionalChats.filter((p) => p.chat_id !== provisional.chat_id), provisional];
      replayedProvisionalIds?.add(provisional.chat_id);
      if (provisional.phase === "failed") {
        settleRegistration(provisional.chat_id, new Error(provisional.error ?? "The chat could not be started"));
      }
      break;
    }
    case "provisional_chat_completed":
      if (event.success) {
        // The chat itself arrives on the chats_updated push, which is what settles waiters.
        provisionalChats = provisionalChats.filter((p) => p.chat_id !== event.chat_id);
      } else if (event.error === null) {
        // Discarded (its tab was closed before it launched): gone, with nothing to show.
        provisionalChats = provisionalChats.filter((p) => p.chat_id !== event.chat_id);
        settleRegistration(event.chat_id, new Error("The chat was closed before it started"));
      } else {
        const error = event.error;
        provisionalChats = provisionalChats.map((p) =>
          p.chat_id === event.chat_id ? { ...p, phase: "failed", error } : p,
        );
        settleRegistration(event.chat_id, new Error(error));
      }
      break;
  }
}

function settleRegistration(chatId: string, error: Error | null): void {
  const waiters = registrationWaiters.get(chatId);
  if (waiters === undefined) return;
  registrationWaiters.delete(chatId);
  for (const waiter of waiters) {
    if (error === null) waiter.resolve();
    else waiter.reject(error);
  }
}

/**
 * Resolves once ``chatId`` is a chat the app lists: at once for one it already lists, and
 * for a chat still being created when its create lands. Rejects, with the reason, when the
 * create fails or the chat is discarded first -- at once for a chat whose create has already
 * failed, since nothing but a retry could ever land it. What a send typed into a chat that
 * does not exist yet waits on.
 *
 * A chat the app neither lists nor holds a provisional record for (destroyed while its page
 * was open, a stale URL) resolves at once too: no push is coming that could settle it, and the
 * send itself reports the backend's refusal. A held send is released the same way when a
 * reconnect's replay turns out not to carry the chat's record any more.
 */
export function whenChatRegistered(chatId: string): Promise<void> {
  const provisional = getProvisionalChat(chatId);
  if (getChatById(chatId) !== undefined || provisional === undefined) return Promise.resolve();
  if (provisional.phase === "failed") {
    return Promise.reject(new Error(provisional.error ?? "The chat could not be started"));
  }
  return new Promise((resolve, reject) => {
    const waiters = registrationWaiters.get(chatId) ?? [];
    waiters.push({ resolve, reject });
    registrationWaiters.set(chatId, waiters);
  });
}

export function initChats(): void {
  connect();
}

export function isConnected(): boolean {
  return connected;
}

/** Every chat the app lists: the backend keeps the workspace's services-only "primary" agent
 *  out of the snapshots it pushes, so nothing here needs filtering. */
export function getChats(): ChatSnapshot[] {
  return chats;
}

export function getChatById(chatId: string): ChatSnapshot | undefined {
  return chats.find((c) => c.chat_id === chatId);
}

/** The full snapshot of the messages queued on the chat's active agent, in enqueue order. */
export function getQueuedMessagesForChat(chatId: string): QueuedMessage[] {
  return getChatById(chatId)?.active_agent.queued_messages ?? [];
}

/** Whether the shoulder-tap is available for this chat, per the backend. */
export function getShoulderTapAvailableForChat(chatId: string): boolean {
  return getChatById(chatId)?.active_agent.shoulder_tap_available === true;
}

/** The provisional record of ``chatId``, while the app lists it as one. */
export function getProvisionalChat(chatId: string): ProvisionalChat | undefined {
  return provisionalChats.find((p) => p.chat_id === chatId);
}

export function addChatsUpdatedListener(listener: ChatsUpdatedListener): void {
  chatsUpdatedListeners.push(listener);
}

export function removeChatsUpdatedListener(listener: ChatsUpdatedListener): void {
  chatsUpdatedListeners = chatsUpdatedListeners.filter((l) => l !== listener);
}

export function addChatActivityListener(listener: ChatActivityListener): void {
  chatActivityListeners.push(listener);
}

export function removeChatActivityListener(listener: ChatActivityListener): void {
  chatActivityListeners = chatActivityListeners.filter((l) => l !== listener);
}

/** The terminal app's origin, where the chat's terminal back face is served from: derived
 *  from the label the chat app read out of the registry into the page. */
export function getTerminalUrl(): string {
  return deriveAppOrigin(getTerminalOriginLabel() || "terminal");
}

/** Build the iframe URL that attaches a terminal to ``agentName``'s tmux session. The terminal
 *  app's dispatch takes the URL's ``arg`` values in order: a placeholder ("_", which lands in
 *  ``$0``), the dispatch key ("agent"), then the agent name.
 *
 *  Only the back face of that agent's chat attaches one: two live ttyd clients on one tmux
 *  window keep resizing it out from under each other. */
export function buildAgentTerminalUrl(agentName: string): string {
  const baseUrl = getTerminalUrl();
  const separator = baseUrl.includes("?") ? "&" : "?";
  return `${baseUrl}${separator}arg=_&arg=agent&arg=${encodeURIComponent(agentName)}`;
}

/** A freshly-created chat's identity: its id and its name pair. */
export interface CreatedChat {
  chatId: string;
  name: string;
  displayName: string;
}

/**
 * Start a chat, returning the id it will be known by and its name pair.
 *
 * The create returns as soon as the chat has an id: its agent is still starting (the chat
 * shows up as provisional until mngr registers it). The display name is minted server-side.
 * ``projectId`` becomes the agent's ``project`` label and is empty for a chat started outside
 * any project. Throws with the server's detail on rejection.
 */
export function createChat(projectId: string, accountId: string = ""): Promise<CreatedChat> {
  // No harness: the account decides it. An empty account_id takes the most recently used account.
  return postCreateChat({ project_id: projectId, account_id: accountId });
}

/**
 * Launch a chat minted earlier (one that waited for an account, or one whose create failed)
 * on ``accountId``: it keeps its id and name, so the tab showing it becomes the chat.
 */
export function launchChat(chatId: string, accountId: string): Promise<CreatedChat> {
  return postCreateChat({ chat_id: chatId, account_id: accountId });
}

async function postCreateChat(body: Record<string, string>): Promise<CreatedChat> {
  const response = await fetch(apiUrl("/api/chats/create"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(data.detail ?? `HTTP ${response.status}`);
  }
  const created = (await response.json()) as { chat_id?: string; name?: string; display_name?: string };
  if (!created.chat_id) {
    throw new Error("Chat creation returned no chat id");
  }
  return {
    chatId: created.chat_id,
    name: created.name ?? "",
    displayName: created.display_name ?? created.name ?? "",
  };
}
