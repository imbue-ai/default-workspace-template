/**
 * Unified WebSocket-based agent and app state manager.
 * Receives real-time updates for agents, apps, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { asChatId } from "../ids";
import type { ChatId } from "../ids";
import { deriveServiceOrigin } from "../origin";
import { ReconnectBackoff } from "./backoff";
import { getActiveProjectId, getClientId, getDeviceKind } from "./ClientIdentity";
import type { ModelChoice } from "./ModelSettings";
import { noteBackendArrivals } from "./OutgoingMessages";
import { parseJsonMessage } from "./ws-json";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  // The mngr ``project`` label, lifted out of ``labels`` by the backend: the
  // project this chat was created in, which mngr propagates to the agent's own
  // children. Null when the agent carries no label. Membership is many-to-many
  // and this label names the chat's *originating* project only -- what a view
  // shows is its member list, not this -- so it is a starting point for filing
  // a chat, never an owner.
  project?: string | null;
  // The mngr ``display_name`` label, lifted out of ``labels`` by the backend:
  // the human-readable name mngr holds for this agent, as the user typed it.
  // Its canonical form (specials dropped, space runs collapsed to dashes) is
  // the true ``name`` above, which stays the only way to address the agent.
  // Null when mngr holds no such label, in which case ``name`` is all there is.
  display_name?: string | null;
  work_dir: string | null;
  // The agent's harness ("claude", "codex", ...), from the backend. Used only as
  // a lookup key into the per-harness catalog (GET /api/harnesses): ModelBar picks
  // the model catalog with it, and the composer guards, fast-mode prompt, and
  // agent-auth surface all follow the popups/auth declarations the catalog
  // carries. The frontend never branches on the harness name itself; everything
  // else is resolved backend-side (tool calls arrive pre-labelled, activity
  // arrives as an already-derived state).
  harness?: string;
  // Per-agent chat activity. THINKING/TOOL_RUNNING/IDLE, or null when the
  // system interface has no per-agent activity tracking available (e.g.
  // remote agents whose state directory is not present on this host,
  // proto-agents).
  activity_state?: string | null;
  // The agent's live model/effort/fast selection plus the catalog option it
  // matched, pushed by the backend beside activity_state. Null when no model
  // resolution is available. Drives the composer's model bar.
  model_choice?: ModelChoice | null;
  // Full snapshot of the messages currently parked in the agent's harness queue,
  // in enqueue order, pushed by the backend beside activity_state. The chat panel
  // renders the queued group from this; it is replaced wholesale on each push and
  // the frontend holds no queued state of its own. Absent/empty when nothing is
  // queued. Mirrors the backend ``QueuedMessageState`` (models.py) -- keep in step.
  queued_messages?: QueuedMessage[];
  // Backend-computed shoulder-tap availability (contract Shoulder-tap): true iff something is
  // queued AND no send is in flight. This ALONE drives the shoulder-tap button's enabled state
  // -- the frontend computes nothing about availability and there is no error path. Absent =
  // treat as unavailable (defensive; the button only renders when the queue is non-empty anyway).
  shoulder_tap_available?: boolean;
  // The stable id of the CHAT this agent currently backs, which is the id of the
  // first agent the chat ever had. Equal to ``id`` for a chat that has never
  // switched harness (and absent on an older backend), which is why every reader
  // goes through ``chatIdOfAgent`` rather than reading it directly.
  chat_id?: string;
  // The harness switch running on this chat right now, pushed by the backend, or
  // absent when none is. Cleared as the last act of a switch, so its absence is
  // the only "the switch is over" signal the frontend needs.
  handoff?: HandoffState | null;
}

/** The state of one in-flight harness switch (the wire shape of the backend
 *  ``HandoffState``). Reported on the agent currently backing the chat. */
export interface HandoffState {
  phase: "preparing" | "finishing" | "failed";
  target_harness: string;
  /** Why a failed switch failed; empty otherwise. */
  detail?: string;
}

/** The chat this agent backs. Falls back to the agent's own id, which is the
 *  right answer for a chat that has never switched -- and for a backend that
 *  does not send the field at all. */
export function chatIdOfAgent(agent: AgentState): ChatId {
  return asChatId(agent.chat_id ?? agent.id);
}

/** One message currently parked in an agent's harness queue (the wire shape of
 *  the backend ``QueuedMessageState``). The frontend renders these verbatim and
 *  keys the bubble on ``queued_id``; it never derives or reconciles them. */
export interface QueuedMessage {
  queued_id: string;
  content: string;
  timestamp: string;
  // True while the backend is actively re-sending this chip (a codex shoulder-tap's
  // interrupt+resend): it stays continuously visible but renders "Sending…" rather than as a
  // plain queued chip, so it never blinks out (contract A1a). Absent/false = a plain queued chip.
  is_sending?: boolean;
}

export interface AppEntry {
  name: string;
  url: string;
  // The unguessable ``<name>-<rand>`` hostname label this service's public
  // origin uses (see ``system/scripts/forward_port.py``). Empty for legacy
  // rows written before labels existed; ``labelForService`` falls back to the
  // name in that case.
  // CLEANUP: treat an empty label as an error (and drop labelForService's
  // legacy-row fallback) once no supported workspace's apps.toml predates
  // minds-v0.3.12, the first release whose forward_port.py mints labels --
  // services re-register (and mint) on boot, so any workspace booted on
  // >=0.3.12 has labeled rows.
  label: string;
  // The app's own icon as SVG markup (a single ``<svg>`` element), registered
  // by the app via ``forward_port.py --icon`` and validated there and again by
  // the backend before it is sent here. Empty for apps that registered no
  // icon, which is the common case: callers fall back to the generic app
  // glyph.
  icon?: string;
  // Registered with ``forward_port.py --internal``: has a port to forward but
  // no page of its own (e.g. owner-exec, a loopback exec server the share
  // gateway routes through). Consumers offering apps to open must exclude it.
  internal?: boolean;
  // The supervisord program running this app, registered via ``forward_port.py
  // --program``. Its presence is what makes the app stoppable/startable from
  // the workspace; absent/empty means unsupervised (or registered before the
  // field existed).
  program?: string;
  // Backend-derived liveness: supervisord's process state for ``program``
  // rows, a TCP probe of the backend URL otherwise. Absent reads as running
  // (see ``isAppRunning``).
  is_running?: boolean;
}

// A live tmux terminal session (any tmux session whose name does NOT start
// with the mngr agent prefix). tmux is the source of truth for terminals:
// these are enumerated straight from ``tmux ls`` by the backend, so a session
// created from the UI, an agent, or a raw ``tmux new-session`` all show up
// identically here.
export interface TerminalSessionInfo {
  session_name: string;
  // The immutable tmux ``#{session_id}`` (e.g. ``$3``); survives a rename, so
  // it's the stable key for reflecting a renamed session back onto its tab.
  session_id: string;
  cwd: string;
}

export interface ProtoAgent {
  agent_id: string;
  name: string;
  creation_type: "chat";
  parent_agent_id: string | null;
}

// Names of the layout-mutation ops the agent-facing ``system/scripts/layout.py``
// helper can emit. The frontend dispatches on this in DockviewWorkspace.
export type LayoutOpName =
  | "open"
  | "focus"
  | "split"
  | "close"
  | "move"
  | "rename"
  | "maximize"
  | "restore"
  | "replace-url"
  | "refresh"
  | "reload_system_interface";

export interface LayoutOpEvent {
  op: LayoutOpName;
  // Op-specific arguments. Shape is verified at the call site (DockviewWorkspace)
  // rather than at the listener boundary -- the WS broadcast is the source of
  // truth and ``system/scripts/layout.py`` enforces shape before broadcasting.
  args: Record<string, unknown>;
  // ``MNGR_AGENT_ID`` of the agent that invoked ``system/scripts/layout.py``. Empty
  // string when the caller did not set ``MNGR_AGENT_ID``. Used to anchor
  // splits against the requester's own chat panel and to resolve the ``self``
  // ref.
  requesterAgentId: string;
}

type WsEvent =
  | { type: "agents_updated"; agents: AgentState[] }
  | { type: "apps_updated"; apps: AppEntry[] }
  | {
      type: "proto_agent_created";
      agent_id: string;
      name: string;
      creation_type: string;
      parent_agent_id: string | null;
    }
  | { type: "proto_agent_completed"; agent_id: string; success: boolean; error: string | null }
  | {
      type: "layout_op";
      op: LayoutOpName;
      args: Record<string, unknown>;
      requester_agent_id?: string;
    }
  | {
      // A terminal tab's underlying tmux session changed: the client attached
      // to a different session (``terminal_id`` set, tmux client-session-changed
      // hook) or a session was renamed (``terminal_id`` null, tmux
      // session-renamed hook -- match on ``session_id`` instead).
      type: "terminal_session";
      terminal_id: string | null;
      session_id: string;
      session_name: string;
    }
  | {
      // An agent asked a client (or all clients, target null) to switch to a
      // view so subsequent layout ops can target it.
      type: "load_layout";
      layout_slug: string;
      display_name: string;
      target_client_id: string | null;
    }
  | {
      // A project's content was saved (by any client). Clients mounted on that
      // project (other than the saver) on the same device kind re-apply it;
      // everyone re-lists.
      type: "project_saved";
      project_id: string;
      saved_by_client_id: string;
      device: string;
    }
  | {
      // A project was deleted; clients mounted on it switch to the fallback.
      type: "project_deleted";
      project_id: string;
      fallback_id: string;
    }
  | {
      // A project's display metadata (name / color / glyph) changed. The
      // content is untouched, so consumers only re-list.
      type: "project_updated";
      project_id: string;
    }
  | {
      // One or more projects' member lists changed (an add, a remove, or a
      // share). Membership is durable and independent of the layout, so this
      // reaches clients that do not have any of these projects mounted.
      type: "project_members_changed";
      project_ids: string[];
    }
  | {
      // A destroyed object's panel was stripped from these views' saved
      // content (Everything included -- its id is "everything"). Any client
      // still showing the panel drops it: the object behind it is gone
      // machine-wide, and a live dock that kept it would autosave the dead
      // panel straight back into the file the server just stripped.
      // ``panel_id`` is null for kinds whose panel ids are minted per open
      // (browser and app panes); ``ref`` is null when the destroyer knew only
      // the panel -- each client resolves whichever it has against its own
      // dock.
      type: "project_panel_removed";
      panel_id: string | null;
      ref: string | null;
      project_ids: string[];
    }
  | {
      // One object was renamed, machine-wide; ``title`` is null when its name
      // was cleared (or dropped because the object was destroyed). A name
      // belongs to the object rather than to a panel, so this reaches clients
      // showing it in a project this one never opened -- and clients listing it
      // backgrounded, with no panel at all.
      type: "member_title_changed";
      ref: string;
      title: string | null;
    }
  | {
      // One object was used, machine-wide; ``at_ms`` is the epoch-millisecond
      // moment the server stamped, or null when the entry was dropped because
      // the object was destroyed. Recency belongs to the object rather than to
      // a panel, so this reaches clients offering it in a launcher this one
      // never opened.
      type: "member_last_used_changed";
      ref: string;
      at_ms: number | null;
    }
  | {
      // One object beaconed where it is looking, machine-wide; ``path`` is
      // null when the entry was dropped (the object was destroyed, or it
      // beaconed a blank). A location belongs to the object rather than to a
      // panel, so this reaches clients that could open it anywhere.
      type: "member_location_changed";
      ref: string;
      path: string | null;
    };

/** Agent-driven view-switch requests pushed over the WebSocket (the ``load``
 *  layout op). The slug names a view: a project id, or Everything. */
export type LayoutSyncEvent = { kind: "load"; layoutSlug: string; displayName: string; targetClientId: string | null };

export type LayoutSyncListener = (event: LayoutSyncEvent) => void;

/**
 * Project registry / sync events pushed over the WebSocket. The mirror of
 * ``LayoutSyncEvent`` for projects: ``saved`` carries the writer's client id so
 * the originator can skip its own echo, ``deleted`` carries the id everyone
 * mounted on it should fall back to, ``updated`` is display metadata only, and
 * ``members`` names every project whose member list moved -- which any client
 * has to act on, mounted on those projects or not, since membership is what
 * the sidebar lists rather than the layout. ``panel_removed`` says a destroyed
 * object's panel was stripped from the named views' saved content (Everything
 * included), so a client still showing that panel drops it from its live dock
 * -- resolving by ``ref`` as well as by ``panelId``, since a browser or app
 * pane's id is minted per open and differs from client to client.
 */
export type ProjectSyncEvent =
  | { kind: "saved"; projectId: string; savedByClientId: string; device: string }
  | { kind: "deleted"; projectId: string; fallbackId: string }
  | { kind: "updated"; projectId: string }
  | { kind: "members"; projectIds: string[] }
  | { kind: "panel_removed"; panelId: string | null; ref: string | null; projectIds: string[] };

export type ProjectSyncListener = (event: ProjectSyncEvent) => void;

/**
 * Notified when one object's machine-wide name changed; ``title`` is null when
 * the object is unnamed again. Delivered to every client, mounted on a view
 * showing the object or not, because a name is a fact about the machine rather
 * than about any one view's layout.
 */
export type MemberTitleListener = (ref: string, title: string | null) => void;

/**
 * Notified when one object's machine-wide recency changed; ``atMs`` is null
 * when the object was destroyed and its entry dropped. Delivered to every
 * client, mounted on a view showing the object or not, because recency is a
 * fact about the machine rather than about any one view's layout.
 */
export type MemberLastUsedListener = (ref: string, atMs: number | null) => void;

/**
 * Notified when one object's machine-wide beaconed location changed; ``path``
 * is null when the entry was dropped. Delivered to every client, because a
 * location is a fact about the machine rather than about any one view.
 */
export type MemberLocationListener = (ref: string, path: string | null) => void;

export type LayoutOpListener = (event: LayoutOpEvent) => void;
export type AgentsUpdatedListener = (agents: AgentState[]) => void;
/**
 * Notified when a terminal tab's underlying tmux session changes (attached to
 * a different session, or the session was renamed). ``terminalId`` is the
 * per-tab id we pass into the ttyd URL when set (client-session-changed);
 * ``null`` for a rename, where the tab is matched on ``sessionId`` instead.
 */
export type TerminalSessionListener = (terminalId: string | null, sessionId: string, sessionName: string) => void;
/**
 * Notified when a single agent's ``activity_state`` changes between two
 * consecutive ``agents_updated`` snapshots. ``previous`` is ``null`` when the
 * agent had no prior tracked state (it just appeared, or its state was
 * untracked). Computed here, in the agent-state authority, so consumers can act
 * on a transition (e.g. working -> IDLE) without keeping their own shadow copy
 * of the previous state.
 */
export type AgentActivityListener = (agentId: string, previous: string | null, current: string | null) => void;

let agents: AgentState[] = [];
// The JSON of the last agents_updated payload, to skip redundant identical pushes.
let lastAgentsSerialized = "";
let apps: AppEntry[] = [];
// Whether an ``apps_updated`` carrying at least one registered service has
// arrived. Until it has, ``labelForService`` cannot resolve a name to its
// unguessable origin label -- which is fine locally (the bare name routes
// through the forwarder) but produces an unroutable ``<name>.<domain>`` origin
// on a share (only ``<label>.<domain>`` is claimed on the relay), yielding a
// 403. Callers that build share-critical origins wait via ``whenAppsLoaded``.
let appsLoaded = false;
let appsLoadedWaiters: (() => void)[] = [];
let protoAgents: ProtoAgent[] = [];
let layoutOpListeners: LayoutOpListener[] = [];
let layoutSyncListeners: LayoutSyncListener[] = [];
let projectSyncListeners: ProjectSyncListener[] = [];
let memberTitleListeners: MemberTitleListener[] = [];
let memberLastUsedListeners: MemberLastUsedListener[] = [];
let memberLocationListeners: MemberLocationListener[] = [];
let agentsUpdatedListeners: AgentsUpdatedListener[] = [];
let terminalSessionListeners: TerminalSessionListener[] = [];
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
  if (ws !== null) {
    return;
  }

  const url = getWsUrl();
  console.info(`[si-ws] connecting to ${url}`);
  ws = new WebSocket(url);

  ws.onopen = () => {
    connected = true;
    console.info("[si-ws] connected");
    // A successful connection resets the backoff so the next disconnect
    // starts from the base delay again.
    reconnectBackoff.reset();
    // Register this browser's identity + active layout with the server so
    // layout-targeted ops can find it. During startup the active layout may
    // not be chosen yet; DockviewWorkspace re-reports once it is.
    reportClientState();
    m.redraw();
  };

  ws.onmessage = (event: MessageEvent) => {
    const data = parseJsonMessage<WsEvent>(event.data as string);
    if (data === null) {
      return;
    }
    handleEvent(data);
    m.redraw();
  };

  ws.onclose = (event: CloseEvent) => {
    console.warn(
      `[si-ws] closed (code=${event.code} reason=${JSON.stringify(event.reason)} wasClean=${event.wasClean})`,
    );
    ws = null;
    connected = false;
    scheduleReconnect();
    m.redraw();
  };

  ws.onerror = () => {
    console.warn("[si-ws] socket error");
    ws?.close();
  };
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) {
    return;
  }
  const delayMs = reconnectBackoff.nextDelay();
  console.info(`[si-ws] reconnecting in ${delayMs}ms`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delayMs);
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "agents_updated": {
      // The backend can broadcast the same snapshot many times during a turn (transcript
      // churn), and a redraw on each identical push makes the model bar (its trusted-HTML
      // logo especially) visibly flicker. Skip a push byte-identical to the last one: an
      // idempotent update should cause no re-render.
      const serialized = JSON.stringify(event.agents);
      if (serialized === lastAgentsSerialized) {
        break;
      }
      lastAgentsSerialized = serialized;
      // Diff against the outgoing snapshot (still in `agents` here) so we can
      // report per-agent activity transitions before replacing it. No separate
      // previous-state bookkeeping is needed -- the prior array is the record.
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
        // Route a newly-queued message through the optimistic-send layer so its
        // "Sending…" bubble drops the instant it becomes a real queued entry
        // (no overlap). Deduped by queued_id, so re-pushed snapshots are harmless.
        const queuedIds = (agent.queued_messages ?? []).map((queued) => queued.queued_id);
        if (queuedIds.length > 0) {
          // Boundary cast: the WS payload carries the physical agent id, which is
          // (today) the same value as the chat's id.
          noteBackendArrivals(asChatId(agent.id), queuedIds);
        }
      }
      break;
    }

    case "apps_updated":
      apps = event.apps;
      // The first non-empty app list means service labels are now resolvable;
      // release anyone waiting on ``whenAppsLoaded``.
      if (!appsLoaded && apps.length > 0) {
        appsLoaded = true;
        const waiters = appsLoadedWaiters;
        appsLoadedWaiters = [];
        for (const wake of waiters) wake();
      }
      break;

    case "proto_agent_created":
      protoAgents.push({
        agent_id: event.agent_id,
        name: event.name,
        creation_type: event.creation_type as "chat",
        parent_agent_id: event.parent_agent_id,
      });
      break;

    case "proto_agent_completed": {
      protoAgents = protoAgents.filter((p) => p.agent_id !== event.agent_id);
      break;
    }

    case "layout_op":
      for (const listener of layoutOpListeners) {
        listener({
          op: event.op,
          args: event.args,
          requesterAgentId: event.requester_agent_id ?? "",
        });
      }
      break;

    case "terminal_session":
      for (const listener of terminalSessionListeners) {
        listener(event.terminal_id, event.session_id, event.session_name);
      }
      break;

    case "load_layout":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "load",
          layoutSlug: event.layout_slug,
          displayName: event.display_name,
          targetClientId: event.target_client_id,
        });
      }
      break;

    case "project_saved":
      for (const listener of projectSyncListeners) {
        listener({
          kind: "saved",
          projectId: event.project_id,
          savedByClientId: event.saved_by_client_id,
          device: event.device,
        });
      }
      break;

    case "project_deleted":
      for (const listener of projectSyncListeners) {
        listener({
          kind: "deleted",
          projectId: event.project_id,
          fallbackId: event.fallback_id,
        });
      }
      break;

    case "project_updated":
      for (const listener of projectSyncListeners) {
        listener({ kind: "updated", projectId: event.project_id });
      }
      break;

    case "project_members_changed":
      for (const listener of projectSyncListeners) {
        listener({ kind: "members", projectIds: event.project_ids });
      }
      break;

    case "project_panel_removed":
      for (const listener of projectSyncListeners) {
        listener({
          kind: "panel_removed",
          panelId: event.panel_id ?? null,
          ref: event.ref ?? null,
          projectIds: event.project_ids,
        });
      }
      break;

    case "member_title_changed":
      for (const listener of memberTitleListeners) {
        listener(event.ref, event.title);
      }
      break;

    case "member_last_used_changed":
      for (const listener of memberLastUsedListeners) {
        listener(event.ref, event.at_ms);
      }
      break;

    case "member_location_changed":
      for (const listener of memberLocationListeners) {
        listener(event.ref, event.path);
      }
      break;
  }
}

/**
 * Report this browser's identity and active layout to the server over the
 * WebSocket (a `client_state` message). Called on WS open and whenever the
 * active layout changes; `previousLayoutSlug` is set on a switch so the
 * server can record a layout_switch event. No-op while the socket is down
 * or before an active layout has been chosen -- the next open re-reports.
 */
export function reportClientState(previousLayoutSlug?: string): void {
  const activeLayout = getActiveProjectId();
  if (ws === null || ws.readyState !== WebSocket.OPEN || !activeLayout) {
    console.info(
      `[si-ws] client_state not sent (readyState=${ws === null ? "no-socket" : ws.readyState} layout=${JSON.stringify(activeLayout)})`,
    );
    return;
  }
  console.info(`[si-ws] sending client_state (client_id=${getClientId()} layout=${activeLayout})`);
  ws.send(
    JSON.stringify({
      type: "client_state",
      client_id: getClientId(),
      active_layout: activeLayout,
      device_kind: getDeviceKind(),
      previous_layout: previousLayoutSlug ?? "",
    }),
  );
}

export function initAgentManager(): void {
  connect();
}

export function isConnected(): boolean {
  return connected;
}

/**
 * Returns true when the agent is the workspace's services-only "primary"
 * agent (window 0 is sleep-infinity; bootstrap + services run in extra
 * tmux windows). These agents are hidden from the user-facing agent list
 * because destroying them would tear down the whole workspace.
 */
export function isPrimaryAgent(agent: AgentState): boolean {
  return agent.labels?.is_primary === "true";
}

export function getAgents(): AgentState[] {
  // Filter at the data layer so every consumer (Dockview list, chat panel,
  // create-agent modal, etc.) sees the same set without duplicating the
  // filter logic. The raw list is still kept internally for callsites that
  // need it (none today, but kept symmetric with getAgentById).
  return agents.filter((a) => !isPrimaryAgent(a));
}

export function getAgentById(id: string): AgentState | undefined {
  return agents.find((a) => a.id === id);
}

/**
 * The agent currently backing a chat, which is not always the agent whose id the chat carries.
 *
 * For everything that goes through the backend this is unnecessary -- the ``/api/chats`` routes
 * resolve the chat themselves. It is here for the few things the frontend has to bind to the
 * PROCESS: a tmux session name, an agent's live state. Those follow a harness switch only if
 * they are looked up by chat.
 */
export function getAgentForChat(chatId: ChatId): AgentState | undefined {
  return agents.find((a) => chatIdOfAgent(a) === chatId);
}

/** The full snapshot of an agent's currently-queued messages, in enqueue order.
 *  Empty when nothing is queued (or the agent is unknown). The single source of
 *  queued state -- the frontend never invents or reconciles it. */
export function getQueuedMessagesForAgent(agentId: string): QueuedMessage[] {
  return getAgentById(agentId)?.queued_messages ?? [];
}

/** Whether the shoulder-tap is available for this agent, per the backend (contract
 *  Shoulder-tap). This ALONE drives the button's enabled state; the frontend computes
 *  nothing. Absent -> unavailable. */
export function getShoulderTapAvailableForAgent(agentId: string): boolean {
  return getAgentById(agentId)?.shoulder_tap_available === true;
}

export function removeAgentLocally(agentId: string): void {
  agents = agents.filter((a) => a.id !== agentId);
}

export function getApps(): AppEntry[] {
  return apps;
}

/** Resolve a service NAME to the unguessable hostname LABEL its public origin
 *  uses. Services register a ``<name>-<rand>`` label (see
 *  ``system/scripts/forward_port.py``); every panel origin is built from that
 *  label, not the bare name. Falls back to the name itself when the service
 *  has no known label -- an unregistered service, the ``system_interface``
 *  shell, or before the app list has loaded -- so origin derivation still
 *  works. */
export function labelForService(name: string): string {
  const app = apps.find((a) => a.name === name);
  if (app?.label) return app.label;
  // No label for this name. Two cases: (1) the app list has not loaded yet --
  // a race the share-critical callers avoid by awaiting ``whenAppsLoaded``, so
  // warn loudly if it happens anyway; (2) the app list is loaded but this name
  // is genuinely unregistered (the shell itself, or a legacy row). Either way
  // the only fallback is the bare name, which routes locally but not on a
  // share -- so this is a last resort, not a silent default.
  // CLEANUP: narrow case (2) to the shell / unregistered names only (no
  // legacy label-less rows) once no supported workspace's apps.toml predates
  // minds-v0.3.12, the first release whose forward_port.py mints labels.
  if (!appsLoaded) {
    console.warn(
      `[si] labelForService("${name}") fell back to the bare name because the app list has not loaded yet; ` +
        "the resulting origin will not route on a shared workspace. Await whenAppsLoaded() before deriving share origins.",
    );
  }
  return name;
}

/** Whether the workspace's app list (service origin labels) has loaded. */
export function areAppsLoaded(): boolean {
  return appsLoaded;
}

/** Resolve once the app list has loaded so ``labelForService`` can return real
 *  origin labels, or after ``timeoutMs`` as a fallback so a workspace that
 *  never reports any app still proceeds (degrading to bare-name origins, which
 *  route locally). Share-critical URL construction (layout restore) awaits this
 *  so a restored terminal/service tab never mounts an unroutable bare-name
 *  origin on a share. */
export function whenAppsLoaded(timeoutMs = 5000): Promise<void> {
  if (appsLoaded) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const wake = (): void => {
      if (settled) return;
      settled = true;
      resolve();
    };
    appsLoadedWaiters.push(wake);
    setTimeout(wake, timeoutMs);
  });
}

export function getProtoAgents(): ProtoAgent[] {
  return protoAgents;
}

export function addLayoutOpListener(listener: LayoutOpListener): void {
  layoutOpListeners.push(listener);
}

export function removeLayoutOpListener(listener: LayoutOpListener): void {
  layoutOpListeners = layoutOpListeners.filter((l) => l !== listener);
}

export function addLayoutSyncListener(listener: LayoutSyncListener): void {
  layoutSyncListeners.push(listener);
}

export function removeLayoutSyncListener(listener: LayoutSyncListener): void {
  layoutSyncListeners = layoutSyncListeners.filter((l) => l !== listener);
}

export function addProjectSyncListener(listener: ProjectSyncListener): void {
  projectSyncListeners.push(listener);
}

export function removeProjectSyncListener(listener: ProjectSyncListener): void {
  projectSyncListeners = projectSyncListeners.filter((l) => l !== listener);
}

export function addMemberTitleListener(listener: MemberTitleListener): void {
  memberTitleListeners.push(listener);
}

export function removeMemberTitleListener(listener: MemberTitleListener): void {
  memberTitleListeners = memberTitleListeners.filter((l) => l !== listener);
}

export function addMemberLastUsedListener(listener: MemberLastUsedListener): void {
  memberLastUsedListeners.push(listener);
}

export function removeMemberLastUsedListener(listener: MemberLastUsedListener): void {
  memberLastUsedListeners = memberLastUsedListeners.filter((l) => l !== listener);
}

export function addMemberLocationListener(listener: MemberLocationListener): void {
  memberLocationListeners.push(listener);
}

export function removeMemberLocationListener(listener: MemberLocationListener): void {
  memberLocationListeners = memberLocationListeners.filter((l) => l !== listener);
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

export function addTerminalSessionListener(listener: TerminalSessionListener): void {
  terminalSessionListeners.push(listener);
}

export function removeTerminalSessionListener(listener: TerminalSessionListener): void {
  terminalSessionListeners = terminalSessionListeners.filter((l) => l !== listener);
}

/** Fetch the live terminal-session fleet (all non-agent tmux sessions) plus
 *  the agent-session prefix. Defensive: returns an empty fleet with the
 *  default prefix if the request fails, so the "+" menu still renders. */
export async function fetchTerminalSessions(): Promise<{ terminals: TerminalSessionInfo[]; prefix: string }> {
  try {
    const response = await fetch(apiUrl("/api/terminals"));
    if (!response.ok) return { terminals: [], prefix: "mngr-" };
    const data = (await response.json()) as { terminals?: TerminalSessionInfo[]; prefix?: string };
    return { terminals: data.terminals ?? [], prefix: data.prefix ?? "mngr-" };
  } catch {
    return { terminals: [], prefix: "mngr-" };
  }
}

// The workspace terminal (ttyd) service lives on its own derived origin
// (``http://terminal.<ws-host>/`` locally). The URL builder below is kept
// here rather than in the view so it is unit-testable without importing
// dockview-core (which needs a DOM).

/** Build the ttyd URL that attaches a tab to a named tmux session via the
 *  ``session`` dispatch key. The ttyd dispatch reads the args positionally:
 *  ``$1`` ("_"), ``$2`` ("session"), ``$3`` (session name), ``$4`` (per-tab id
 *  used for live title tracking), ``$5`` (working dir for a fresh session;
 *  empty falls back to $HOME). ``new-session -A`` attaches if the session
 *  exists and creates it otherwise, which is what makes these terminals
 *  persistent in memory. */
export function buildSessionTerminalUrl(sessionName: string, terminalId: string, workdir: string): string {
  const params = new URLSearchParams();
  params.append("arg", "_");
  params.append("arg", "session");
  params.append("arg", sessionName);
  params.append("arg", terminalId);
  params.append("arg", workdir);
  return `${deriveServiceOrigin(labelForService("terminal"))}?${params.toString()}`;
}

/** Ask the backend to allocate the next free ``terminal-N`` session name. The
 *  backend inspects live tmux sessions and picks the lowest unused index under
 *  a lock, so concurrent "New terminal" clicks get distinct names. */
export async function allocateTerminalName(): Promise<string> {
  const response = await fetch(apiUrl("/api/terminals/allocate"), { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to allocate terminal name (HTTP ${response.status})`);
  }
  const data = (await response.json()) as { session_name?: string };
  if (!data.session_name) {
    throw new Error("Terminal allocation returned no session_name");
  }
  return data.session_name;
}

/** The chat harnesses a create can stack.
 *
 *  These are mngr's own agent type names -- what ``mngr create --type`` resolves
 *  and what the create endpoint validates ``harness`` against. Pi's is
 *  ``pi-coding``; ``pi`` is only an mngr-side alias the endpoint's enum does not
 *  accept. Declared here, beside the request that carries it, so no view has to
 *  restate the set. */
export type ChatHarness = "claude" | "codex" | "pi-coding" | "opencode" | "antigravity";

/** A freshly-created chat agent's identity: its id and its name pair (the
 *  canonical true name plus the human-readable display name the server minted
 *  or accepted). */
export interface CreatedChatAgent {
  agentId: string;
  name: string;
  displayName: string;
}

/**
 * Start a chat agent, returning the id it will be known by and its name pair.
 *
 * The create returns as soon as the agent has an id: the agent itself is still
 * starting (it shows up as a proto agent until mngr registers it), which is
 * what lets a caller open its chat tab immediately. The display name is minted
 * server-side (the first free "Chat N" / "Codex N" / "Pi N" on the machine), so
 * two clients creating at once cannot both mint "Chat 1"; its canonical form is
 * the agent's mngr name and the typed form its ``display_name`` label.
 * ``projectId`` becomes the agent's ``project`` label -- the project the chat
 * was started in, which mngr propagates to its children -- and is empty for a
 * chat started outside any project. Throws with the server's detail on
 * rejection.
 */
export async function createChatAgent(projectId: string, accountId: string = ""): Promise<CreatedChatAgent> {
  const response = await fetch(apiUrl("/api/agents/create-chat"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // No harness: the account decides it, so naming one here could only ever
    // contradict the credential the chat will actually run on. No `first` either --
    // that create template belongs to the workspace's own first run, not to anything
    // a user clicks. An empty account_id takes the most recently used account.
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
