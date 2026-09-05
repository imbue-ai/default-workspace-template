/**
 * The chat pages' live agent state: the agent list (activity, model choice, queued messages)
 * and the proto agents, as the chat app pushes them.
 *
 * The chat app shares the shell's process, so its pages read this off the shell's own
 * WebSocket: the socket sends ``agents_updated`` and the proto-agent events beside the
 * shell's own messages, and this document never registers as a client of it.
 * CLEANUP: point this at the chat app's own socket in phase 10 of the workspace app model,
 * when the chat app runs as its own process and serves its own.
 */

import m from "mithril";
import { apiUrl } from "../../base-path";
import { deriveServiceOrigin } from "../../origin";
import { ReconnectBackoff } from "../../models/backoff";
import type { ModelChoice } from "./ModelSettings";
import { parseJsonMessage } from "../../models/ws-json";

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

export interface ProtoAgent {
  agent_id: string;
  name: string;
  creation_type: "chat";
  parent_agent_id: string | null;
}

// The terminal app's origin label, off the shell's ``apps_updated`` push: the chat's terminal
// back face is served from that origin.
// CLEANUP: phase 10 hands the chat app its own configuration for the terminal origin; until
// then the label rides the shared socket.
interface AppLabelRow {
  name: string;
  label: string;
}

type WsEvent =
  | { type: "agents_updated"; agents: AgentState[] }
  | { type: "apps_updated"; apps: AppLabelRow[] }
  | {
      type: "proto_agent_created";
      agent_id: string;
      name: string;
      creation_type: string;
      parent_agent_id: string | null;
    }
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
let appLabelByName: Record<string, string> = {};
let protoAgents: ProtoAgent[] = [];
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
    case "apps_updated":
      appLabelByName = Object.fromEntries(event.apps.map((app) => [app.name, app.label]));
      break;
    case "proto_agent_created":
      protoAgents.push({
        agent_id: event.agent_id,
        name: event.name,
        creation_type: event.creation_type as "chat",
        parent_agent_id: event.parent_agent_id,
      });
      break;
    case "proto_agent_completed":
      protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
      break;
  }
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

export function getProtoAgents(): ProtoAgent[] {
  return protoAgents;
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

/** The terminal app's origin, where the chat's terminal back face is served from. */
export function getTerminalUrl(): string {
  return deriveServiceOrigin(appLabelByName.terminal ?? "terminal");
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
export async function createChatAgent(projectId: string, accountId: string = ""): Promise<CreatedChatAgent> {
  const response = await fetch(apiUrl("/api/agents/create-chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // No harness: the account decides it. An empty account_id takes the most recently used account.
    body: JSON.stringify({ project_id: projectId, account_id: accountId }),
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
