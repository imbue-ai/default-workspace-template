/**
 * Unified WebSocket-based agent and app state manager.
 * Receives real-time updates for agents, apps, and proto-agents.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { deriveServiceOrigin } from "../origin";
import { ReconnectBackoff } from "./backoff";
import { getActiveLayoutSlug, getClientId, getDeviceKind } from "./ClientIdentity";
import { parseJsonMessage } from "./ws-json";

export interface AgentState {
  id: string;
  name: string;
  state: string;
  labels: Record<string, string>;
  work_dir: string | null;
  // Per-agent chat activity. THINKING/TOOL_RUNNING/IDLE, or null when the
  // system interface has no per-agent activity tracking available (e.g.
  // remote agents whose state directory is not present on this host,
  // proto-agents, non-Claude agent types).
  activity_state?: string | null;
}

export interface AppEntry {
  name: string;
  url: string;
  // The unguessable ``<name>-<rand>`` hostname label this service's public
  // origin uses (see ``system/scripts/forward_port.py``). Empty for legacy
  // rows written before labels existed; ``labelForService`` falls back to the
  // name in that case.
  label: string;
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
  creation_type: "worktree" | "chat";
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
      // A named layout's content was saved (by any client). Clients with the
      // layout active (other than the saver) re-apply it; everyone refreshes
      // their cached layouts list.
      type: "layout_saved";
      layout_slug: string;
      display_name: string;
      saved_by_client_id: string;
    }
  | {
      // A named layout was deleted; clients with it active switch to the
      // fallback.
      type: "layout_deleted";
      layout_slug: string;
      fallback_layout_slug: string;
    }
  | {
      // An agent asked a client (or all clients, target null) to switch to a
      // named layout so subsequent layout ops can target it.
      type: "load_layout";
      layout_slug: string;
      display_name: string;
      target_client_id: string | null;
    };

/** Layout registry / sync events pushed over the WebSocket. */
export type LayoutSyncEvent =
  | { kind: "saved"; layoutSlug: string; displayName: string; savedByClientId: string }
  | { kind: "deleted"; layoutSlug: string; fallbackLayoutSlug: string }
  | { kind: "load"; layoutSlug: string; displayName: string; targetClientId: string | null };

export type LayoutSyncListener = (event: LayoutSyncEvent) => void;

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
let apps: AppEntry[] = [];
// Whether an ``apps_updated`` carrying at least one registered service has
// arrived. Until it has, ``labelForService`` cannot resolve a name to its
// unguessable origin label -- which is fine locally (the bare name routes
// through the forwarder) but produces an unroutable ``<name>.<domain>`` origin
// on a share (only ``<label>.<domain>`` is claimed on the relay), yielding a
// 403. Callers that build share-critical origins wait via ``whenAppsLoaded``.
let appsLoaded = false;
let appsLoadedWaiters: (() => void)[] = [];
// Waiters on one NAMED service appearing, which is a different question from
// ``appsLoaded``: services register independently (each via
// ``forward_port.py``), and the shell itself is one of them, so the list goes
// non-empty as soon as ``system_interface`` registers -- while a slower app is
// still on its way. See ``whenAppRegistered``.
let appRegisteredWaiters: { name: string; wake: (isRegistered: boolean) => void }[] = [];
let protoAgents: ProtoAgent[] = [];
let layoutOpListeners: LayoutOpListener[] = [];
let layoutSyncListeners: LayoutSyncListener[] = [];
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
      // Release anyone waiting on a specific service that this frame carries.
      // Iterated over a copy: each ``wake`` removes its own entry.
      for (const waiter of [...appRegisteredWaiters]) {
        if (apps.some((app) => app.name === waiter.name)) waiter.wake(true);
      }
      break;

    case "proto_agent_created":
      protoAgents.push({
        agent_id: event.agent_id,
        name: event.name,
        creation_type: event.creation_type as "worktree" | "chat",
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

    case "layout_saved":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "saved",
          layoutSlug: event.layout_slug,
          displayName: event.display_name,
          savedByClientId: event.saved_by_client_id,
        });
      }
      break;

    case "layout_deleted":
      for (const listener of layoutSyncListeners) {
        listener({
          kind: "deleted",
          layoutSlug: event.layout_slug,
          fallbackLayoutSlug: event.fallback_layout_slug,
        });
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
  const activeLayout = getActiveLayoutSlug();
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

/** Resolve once the service ``name`` is registered -- true when it is (or
 *  already was), false if it has not appeared within ``timeoutMs``.
 *
 *  ``whenAppsLoaded`` is the wrong signal for this: it reports that the app
 *  LIST is non-empty, not that any particular app is in it. The shell registers
 *  itself (``system_interface``), so that flips at boot no matter which other
 *  services have come up -- a caller asking "is app X here?" right after it
 *  would get a no for an app that is merely seconds behind. Waiting on the name
 *  is what separates "not registered yet" (transient, on a cold workspace) from
 *  "not registered at all" (a bad or stale name), which callers report
 *  differently. */
export function whenAppRegistered(name: string, timeoutMs = 5000): Promise<boolean> {
  if (apps.some((app) => app.name === name)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const wake = (isRegistered: boolean): void => {
      if (settled) return;
      settled = true;
      appRegisteredWaiters = appRegisteredWaiters.filter((waiter) => waiter.wake !== wake);
      resolve(isRegistered);
    };
    appRegisteredWaiters.push({ name, wake });
    setTimeout(() => wake(false), timeoutMs);
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
