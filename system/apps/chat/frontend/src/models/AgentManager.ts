/**
 * The chat pages' live agent state: the agent list (activity, model choice, queued messages)
 * and the provisional chats (minted here, not agents yet), as the chat app pushes them over
 * its own WebSocket (``/api/ws``).
 */

import m from "mithril";
import { apiUrl, getTerminalOriginLabel } from "@imbue/workspace-ui/src/base-path";
import { deriveServiceOrigin } from "@imbue/workspace-ui/src/origin";
import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import type { ModelChoice } from "./ModelSettings";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  // The mngr ``project`` label, lifted out of ``labels`` by the backend: the project this chat
  // was created in, which mngr propagates to the agent's own children. Null when the agent
  // carries no label.
  project?: string | null;
  // The mngr ``display_name`` label, lifted out of ``labels`` by the backend: the
  // human-readable name mngr holds for this agent, as the user typed it. Its canonical form is
  // the true ``name`` above, which stays the only way to address the agent by name.
  display_name?: string | null;
  work_dir: string | null;
  // The agent's harness ("claude", "codex", ...), from the backend. Used only as a lookup key
  // into the per-harness catalog (GET /api/harnesses).
  harness?: string;
  // Per-agent chat activity. THINKING/TOOL_RUNNING/IDLE, or null when the system interface has
  // no per-agent activity tracking available.
  activity_state?: string | null;
  // The agent's live model/effort/fast selection plus the catalog option it matched, pushed by
  // the backend beside activity_state. Null when no model resolution is available.
  model_choice?: ModelChoice | null;
  // Full snapshot of the messages currently parked in the agent's harness queue, in enqueue
  // order. Replaced wholesale on each push; the frontend holds no queued state of its own.
  queued_messages?: QueuedMessage[];
  // Backend-computed shoulder-tap availability: true iff something is queued AND no send is in
  // flight. Absent = treat as unavailable.
  shoulder_tap_available?: boolean;
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
export interface ProtoAgent {
  agent_id: string;
  name: string;
  // The account it launches on; empty while it waits for one.
  account_id: string;
  phase: ProvisionalChatPhase;
  // Why the create failed, in the failed phase.
  error: string | null;
}

type WsEvent =
  | { type: "agents_updated"; agents: AgentState[] }
  | ({ type: "proto_agent_created" } & ProtoAgent)
  | { type: "proto_agent_completed"; agent_id: string; success: boolean; error: string | null };

export type AgentsUpdatedListener = (agents: AgentState[]) => void;
/**
 * Notified when a single agent's ``activity_state`` changes between two consecutive
 * ``agents_updated`` snapshots. ``previous`` is ``null`` when the agent had no prior tracked
 * state (it just appeared, or its state was untracked).
 */
export type AgentActivityListener = (agentId: string, previous: string | null, current: string | null) => void;

let agents: AgentState[] = [];
// The JSON of the last agents_updated payload, to skip redundant identical pushes.
let lastAgentsSerialized = "";
let protoAgents: ProtoAgent[] = [];
// Who is waiting for a provisional chat to become an agent (a send typed while it was being
// created), settled by the push that registers it or the one that fails it.
const registrationWaiters = new Map<string, { resolve: () => void; reject: (error: Error) => void }[]>();
let agentsUpdatedListeners: AgentsUpdatedListener[] = [];
let agentActivityListeners: AgentActivityListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let connected = false;

const reconnectBackoff = new ReconnectBackoff();

function getWsUrl(): string {
  const base = apiUrl("/api/ws");
  const loc = window.location;
  const protocol = loc.protocol === "https:" ? "wss:" : "ws:";
  if (base.startsWith("http")) {
    return base.replace(/^http/, "ws");
  }
  return `${protocol}//${loc.host}${base}`;
}

function connect(): void {
  if (ws !== null) return;
  const url = getWsUrl();
  console.info(`[chat-ws] connecting to ${url}`);
  ws = new WebSocket(url);

  ws.onopen = () => {
    connected = true;
    console.info("[chat-ws] connected");
    reconnectBackoff.reset();
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
    case "agents_updated": {
      // The backend can broadcast the same snapshot many times during a turn (transcript
      // churn), and a redraw on each identical push makes the model bar visibly flicker.
      const serialized = JSON.stringify(event.agents);
      if (serialized === lastAgentsSerialized) break;
      lastAgentsSerialized = serialized;
      // Diff against the outgoing snapshot (still in `agents` here) so per-agent activity
      // transitions can be reported before replacing it.
      const previousActivityById = new Map(agents.map((a) => [a.id, a.activity_state ?? null]));
      agents = event.agents;
      // A provisional chat the list now names is an agent, whatever order the pushes came in.
      const registeredIds = new Set(agents.map((a) => a.id));
      protoAgents = protoAgents.filter((p) => !registeredIds.has(p.agent_id));
      for (const agentId of registeredIds) settleRegistration(agentId, null);
      for (const listener of agentsUpdatedListeners) {
        listener(getAgents());
      }
      for (const agent of agents) {
        const current = agent.activity_state ?? null;
        const previous = previousActivityById.get(agent.id) ?? null;
        if (previous !== current) {
          for (const listener of agentActivityListeners) {
            listener(agent.id, previous, current);
          }
        }
      }
      break;
    }
    case "proto_agent_created": {
      // Also how a chat moves between phases (a reserved chat launched, a failed one retried):
      // the backend pushes the whole record again. A reconnect replays every provisional chat
      // this way too, so a failed record seen here settles a send held for it as the
      // completion message would have.
      const { type: _type, ...proto } = event;
      protoAgents = [...protoAgents.filter((p) => p.agent_id !== proto.agent_id), proto];
      if (proto.phase === "failed") {
        settleRegistration(proto.agent_id, new Error(proto.error ?? "The chat could not be started"));
      }
      break;
    }
    case "proto_agent_completed":
      if (event.success) {
        // The agent itself arrives on the agents_updated push, which is what settles waiters.
        protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
      } else if (event.error === null) {
        // Discarded (its tab was closed before it launched): gone, with nothing to show.
        protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
        settleRegistration(event.agent_id, new Error("The chat was closed before it started"));
      } else {
        const error = event.error;
        protoAgents = protoAgents.map((p) => (p.agent_id === event.agent_id ? { ...p, phase: "failed", error } : p));
        settleRegistration(event.agent_id, new Error(error));
      }
      break;
  }
}

function settleRegistration(agentId: string, error: Error | null): void {
  const waiters = registrationWaiters.get(agentId);
  if (waiters === undefined) return;
  registrationWaiters.delete(agentId);
  for (const waiter of waiters) {
    if (error === null) waiter.resolve();
    else waiter.reject(error);
  }
}

/**
 * Resolves once ``agentId`` is an agent the app lists: at once for one it already lists, and
 * for a chat still being created when its create lands. Rejects, with the reason, when the
 * create fails or the chat is discarded first. What a send typed into a chat that does not
 * exist yet waits on.
 */
export function whenAgentRegistered(agentId: string): Promise<void> {
  if (getAgentById(agentId) !== undefined) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const waiters = registrationWaiters.get(agentId) ?? [];
    waiters.push({ resolve, reject });
    registrationWaiters.set(agentId, waiters);
  });
}

export function initAgentManager(): void {
  connect();
}

export function isConnected(): boolean {
  return connected;
}

/** Whether the agent is the workspace's services-only "primary" agent, which is hidden from the
 *  user-facing agent list because destroying it would tear down the whole workspace. */
export function isPrimaryAgent(agent: AgentState): boolean {
  return agent.labels?.is_primary === "true";
}

export function getAgents(): AgentState[] {
  return agents.filter((a) => !isPrimaryAgent(a));
}

export function getAgentById(id: string): AgentState | undefined {
  return agents.find((a) => a.id === id);
}

/** The full snapshot of an agent's currently-queued messages, in enqueue order. */
export function getQueuedMessagesForAgent(agentId: string): QueuedMessage[] {
  return getAgentById(agentId)?.queued_messages ?? [];
}

/** Whether the shoulder-tap is available for this agent, per the backend. */
export function getShoulderTapAvailableForAgent(agentId: string): boolean {
  return getAgentById(agentId)?.shoulder_tap_available === true;
}

/** The provisional record of ``agentId``, while the app lists it as one. */
export function getProtoAgent(agentId: string): ProtoAgent | undefined {
  return protoAgents.find((p) => p.agent_id === agentId);
}

export function addAgentsUpdatedListener(listener: AgentsUpdatedListener): void {
  agentsUpdatedListeners.push(listener);
}

export function removeAgentsUpdatedListener(listener: AgentsUpdatedListener): void {
  agentsUpdatedListeners = agentsUpdatedListeners.filter((l) => l !== listener);
}

export function addAgentActivityListener(listener: AgentActivityListener): void {
  agentActivityListeners.push(listener);
}

export function removeAgentActivityListener(listener: AgentActivityListener): void {
  agentActivityListeners = agentActivityListeners.filter((l) => l !== listener);
}

/** The terminal app's origin, where the chat's terminal back face is served from: derived
 *  from the label the chat app read out of the registry into the page. */
export function getTerminalUrl(): string {
  return deriveServiceOrigin(getTerminalOriginLabel() || "terminal");
}

/** Build the iframe URL that attaches a terminal to ``agentName``'s tmux session. The ttyd
 *  dispatch reads ``$1`` ("_") then ``$2`` ("agent") then ``$3`` (the agent name).
 *
 *  Only the back face of that agent's chat attaches one: two live ttyd clients on one tmux
 *  window keep resizing it out from under each other. */
export function buildAgentTerminalUrl(agentName: string): string {
  const baseUrl = getTerminalUrl();
  const separator = baseUrl.includes("?") ? "&" : "?";
  return `${baseUrl}${separator}arg=_&arg=agent&arg=${encodeURIComponent(agentName)}`;
}

/** A freshly-created chat agent's identity: its id and its name pair. */
export interface CreatedChatAgent {
  agentId: string;
  name: string;
  displayName: string;
}

/**
 * Start a chat agent, returning the id it will be known by and its name pair.
 *
 * The create returns as soon as the agent has an id: the agent itself is still starting (it
 * shows up as a proto agent until mngr registers it). The display name is minted server-side.
 * ``projectId`` becomes the agent's ``project`` label and is empty for a chat started outside
 * any project. Throws with the server's detail on rejection.
 */
export function createChatAgent(projectId: string, accountId: string = ""): Promise<CreatedChatAgent> {
  // No harness: the account decides it. An empty account_id takes the most recently used account.
  return postCreateChat({ project_id: projectId, account_id: accountId });
}

/**
 * Launch a chat minted earlier (one that waited for an account, or one whose create failed)
 * on ``accountId``: it keeps its id and name, so the tab showing it becomes the chat.
 */
export function launchChat(agentId: string, accountId: string): Promise<CreatedChatAgent> {
  return postCreateChat({ agent_id: agentId, account_id: accountId });
}

async function postCreateChat(body: Record<string, string>): Promise<CreatedChatAgent> {
  const response = await fetch(apiUrl("/api/agents/create-chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const data = (await response.json().catch(() => ({}))) as { detail?: string };
    throw new Error(data.detail ?? `HTTP ${response.status}`);
  }
  const created = (await response.json()) as { agent_id?: string; name?: string; display_name?: string };
  if (!created.agent_id) {
    throw new Error("Chat creation returned no agent id");
  }
  return {
    agentId: created.agent_id,
    name: created.name ?? "",
    displayName: created.display_name ?? created.name ?? "",
  };
}
