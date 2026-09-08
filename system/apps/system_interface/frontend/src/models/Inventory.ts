/**
 * The shell's view of the machine: every registered app with its instances, as the shell's
 * WebSocket pushes it (workspace app model, contracts.md section 8), plus the projects, the
 * agent-driven layout ops, and the tab rebinds that ride the same socket.
 *
 * This is the one socket the shell holds. On connect the server sends ``apps_updated`` and
 * ``projects_updated``; the shell answers with its ``client_state`` (which client it is, on
 * which device, looking at which view) and re-sends it on every view switch. ``layout_updated``
 * and ``active_view_changed`` are how this client's other windows, and the shell's own edits,
 * reach this one (contracts.md section 8).
 *
 * Nothing here knows what any app is: a chat, a terminal and a browser are apps with instances
 * like any other, and the surfaces that draw them read the app's own display name, icon,
 * actions and instance titles off these records.
 */

import { addressFor, parseAddress } from "@imbue/workspace-ui/src/addresses";
import m from "mithril";
import { wsUrl } from "@imbue/workspace-ui/src/base-path";
import { deriveServiceOrigin, workspaceHostCoordinate } from "@imbue/workspace-ui/src/origin";
import { ReconnectBackoff } from "@imbue/workspace-ui/src/models/backoff";
import { getActiveProjectId, getClientId, getDeviceKind } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { parseJsonMessage } from "@imbue/workspace-ui/src/models/ws-json";

/** What an instance is doing, as its app reports it (contracts.md section 4.1). */
export type InstanceStatus = "idle" | "working" | "attention" | "stopped" | "error";

/** Whether an instance lives until deleted, or only while a tab set or layout references it. */
export type InstanceLifetime = "explicit" | "referenced";

/** One instance of an app, as the shell lists it. A single-instance app carries exactly one,
 *  synthesized by the shell with an empty key. */
export interface InstanceRecord {
  key: string;
  /** A path under the app's origin, optionally carrying ``{tab}`` once. */
  url: string;
  title: string;
  status: InstanceStatus;
  lifetime: InstanceLifetime;
  /** ISO timestamp, or null when the app reports none. */
  last_active: string | null;
  renameable: boolean;
  /** Whether the app stops and starts this instance on its own (a chat's agent, a browser's Chromium, a terminal's session). */
  stoppable: boolean;
}

export interface AppAction {
  id: string;
  label: string;
  /** The names of the create body's documented params (contracts.md section 3). */
  params: string[];
}

export type ShortcutMode = "focus" | "new";

export interface DefaultShortcut {
  action: string;
  mode: ShortcutMode;
}

/** One registered app: its registry row plus its liveness and instance list. */
export interface AppRecord {
  name: string;
  display_name: string;
  /** The app's own icon as SVG markup, or "" when it registered none. */
  icon: string;
  /** The unguessable origin label its public origin uses ("" on a legacy row). */
  label: string;
  url: string;
  internal: boolean;
  program: string;
  critical: boolean;
  instances_url: string;
  has_instances: boolean;
  actions: AppAction[];
  default_shortcut: DefaultShortcut | null;
  /** Where the app's tile sits on the New Tab page's leading row, lowest first; null for the second row. */
  launcher_rank: number | null;
  is_running: boolean;
  /** Whether ``instances`` is the app's own answer; false until its list has been fetched once. */
  is_listed: boolean;
  instances: InstanceRecord[];
}

/** An instance found by address: the app it belongs to and the address itself. */
export interface ResolvedInstance {
  app: AppRecord;
  instance: InstanceRecord;
  address: string;
}

/** One rail shortcut of a project (contracts.md section 6). */
export interface ProjectShortcut {
  app: string;
  action: string;
  mode: ShortcutMode;
}

/** A project as the shell lists it: its display metadata, its tab set, and its rail. */
export interface ProjectInfo {
  id: string;
  name: string;
  color: string;
  glyph: number;
  tabs: string[];
  shortcuts: ProjectShortcut[];
}

// The transient layout ops that still reach the browser as messages (contracts.md section 12): the
// verbs that change what is on screen without changing the saved document. Every other op is
// applied by the shell to the layout file and arrives here as a ``layout_updated``.
export type LayoutOpName = "maximize" | "restore" | "refresh" | "reload_system_interface";

export interface LayoutOpEvent {
  op: LayoutOpName;
  args: Record<string, unknown>;
  /** The address of the instance that invoked the helper (its own chat), "" when unknown. */
  requester: string;
}

/** A client layout was written on the shell (a save of ours or another window's, or the shell's own edit). */
export interface LayoutUpdatedEvent {
  viewId: string;
  clientId: string;
  saveId: string;
}

/** A client's stored active view moved (a switch in another window, or an agent's ``load``). */
export interface ActiveViewChangedEvent {
  clientId: string;
  viewId: string;
}

/** An app re-pointed a tab at another of its instances (the terminal app, when a tab's client
 *  switches tmux session): the shell already rewrote the layout file, and the dock re-keys. */
export interface TabReboundEvent {
  clientId: string;
  viewId: string;
  tabId: string;
  address: string;
}

type WsEvent =
  | { type: "apps_updated"; apps: AppRecord[] }
  | { type: "projects_updated"; projects: ProjectInfo[] }
  | {
      type: "layout_op";
      op: LayoutOpName;
      args: Record<string, unknown>;
      requester?: string;
      target_client_id?: string | null;
    }
  | { type: "layout_updated"; view_id: string; client_id: string; save_id: string }
  | { type: "active_view_changed"; client_id: string; view_id: string }
  | { type: "tab_rebound"; client_id: string; view_id: string; tab_id: string; address: string };

export type AppsUpdatedListener = (apps: AppRecord[]) => void;
export type ProjectsUpdatedListener = (projects: ProjectInfo[]) => void;
export type LayoutOpListener = (event: LayoutOpEvent) => void;
export type LayoutUpdatedListener = (event: LayoutUpdatedEvent) => void;
export type ActiveViewChangedListener = (event: ActiveViewChangedEvent) => void;
export type TabReboundListener = (event: TabReboundEvent) => void;

// ---------- Addresses (pure) ----------

// The address helpers are the library's (an app page names addresses too); re-exported so the
// shell's callers keep reading them off the inventory.
export { addressFor, parseAddress };

/** The app an address names, or null when it is not an address. */
export function appNameFromAddress(address: string): string | null {
  return parseAddress(address)?.app ?? null;
}

// ---------- Actions (pure) ----------

/** The action an app's rail row and launcher tile run: its declared default shortcut's action
 *  when the app declares one, else its first action, else the synthesized ``open``. Null for an
 *  app with instances that declares no action at all. */
export function primaryActionForApp(app: AppRecord): AppAction | null {
  if (app.default_shortcut !== null) {
    const declared = app.actions.find((action) => action.id === app.default_shortcut?.action);
    if (declared !== undefined) return declared;
  }
  return app.actions[0] ?? null;
}

// ---------- Liveness (pure) ----------

/** Whether the workspace can stop and start this app: supervised, not critical to the workspace,
 *  and not running inside a critical app's program (the shell refuses those the same way). */
export function isAppStoppable(app: AppRecord): boolean {
  if (app.program === "" || app.critical) return false;
  return !apps.some((other) => other.critical && other.program === app.program);
}

/** Why a stopped app is not answering, in the row's tooltip. */
export function appStoppedDetail(app: AppRecord): string {
  return app.program !== "" ? "stopped" : "not running (managed outside the workspace)";
}

// ---------- Page URLs (pure given a host) ----------

/**
 * Where an instance's page is: its app's origin plus the instance's path, with ``{tab}``
 * filled in from the tab the page is opened in.
 *
 * The origin is the app's, derived from its registered label on a workspace host exactly as
 * every app's is (see origin.ts). A host with no workspace coordinate (a direct hit on the
 * loopback port, the e2e suite) has no origin family to derive into, so the app's registered
 * loopback URL is used instead. ``host`` and ``protocol`` default to this document's own and
 * are parameters so the derivation is unit-testable without a DOM.
 */
export function instancePageUrl(
  app: Pick<AppRecord, "name" | "label" | "url">,
  instance: Pick<InstanceRecord, "url">,
  tabId: string,
  host: string = window.location.host,
  protocol: string = window.location.protocol,
): string {
  const origin =
    workspaceHostCoordinate(host) === host
      ? app.url.replace(/\/$/, "")
      : deriveServiceOrigin(labelForApp(app), host, protocol).replace(/\/$/, "");
  const path = instance.url.split("{tab}").join(encodeURIComponent(tabId));
  return `${origin}${path.startsWith("/") ? path : `/${path}`}`;
}

/** The origin label an app's public origin uses: its registered label, else its name (a legacy row). */
export function labelForApp(app: Pick<AppRecord, "name" | "label">): string {
  return app.label !== "" ? app.label : app.name;
}

// ---------- The socket ----------

let apps: AppRecord[] = [];
let appsLoaded = false;
let appsLoadedWaiters: (() => void)[] = [];
let appsUpdatedListeners: AppsUpdatedListener[] = [];
let projectsUpdatedListeners: ProjectsUpdatedListener[] = [];
let layoutOpListeners: LayoutOpListener[] = [];
let layoutUpdatedListeners: LayoutUpdatedListener[] = [];
let activeViewChangedListeners: ActiveViewChangedListener[] = [];
let tabReboundListeners: TabReboundListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

const reconnectBackoff = new ReconnectBackoff();

function connect(): void {
  if (ws !== null) return;
  const url = wsUrl("/api/ws");
  console.info(`[si-ws] connecting to ${url}`);
  ws = new WebSocket(url);

  ws.onopen = () => {
    console.info("[si-ws] connected");
    reconnectBackoff.reset();
    // During startup the active view may not be chosen yet; the dock re-reports once it is.
    reportClientState();
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
      `[si-ws] closed (code=${event.code} reason=${JSON.stringify(event.reason)} wasClean=${event.wasClean})`,
    );
    ws = null;
    scheduleReconnect();
    m.redraw();
  };

  ws.onerror = () => {
    console.warn("[si-ws] socket error");
    ws?.close();
  };
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  const delayMs = reconnectBackoff.nextDelay();
  console.info(`[si-ws] reconnecting in ${delayMs}ms`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delayMs);
}

function handleEvent(event: WsEvent): void {
  switch (event.type) {
    case "apps_updated":
      applyApps(event.apps);
      break;
    case "projects_updated":
      applyProjects(event.projects);
      break;
    case "layout_op":
      // A targeted op is for one client's windows; an untargeted one (a refresh of a whole app, the
      // interface reload) is for every window.
      if (event.target_client_id != null && event.target_client_id !== getClientId()) break;
      for (const listener of layoutOpListeners) {
        listener({ op: event.op, args: event.args, requester: event.requester ?? "" });
      }
      break;
    case "layout_updated":
      for (const listener of layoutUpdatedListeners) {
        listener({ viewId: event.view_id, clientId: event.client_id, saveId: event.save_id });
      }
      break;
    case "active_view_changed":
      for (const listener of activeViewChangedListeners) {
        listener({ clientId: event.client_id, viewId: event.view_id });
      }
      break;
    case "tab_rebound":
      for (const listener of tabReboundListeners) {
        listener({
          clientId: event.client_id,
          viewId: event.view_id,
          tabId: event.tab_id,
          address: event.address,
        });
      }
      break;
  }
}

/** Feed one socket message through the handler. Test-only. */
export function dispatchSocketEventForTesting(event: WsEvent): void {
  handleEvent(event);
}

/** Take a pushed (or fetched) app list. Exported so tests can seed the inventory without a socket. */
export function applyApps(next: AppRecord[]): void {
  apps = next;
  // The first non-empty list means origin labels are resolvable; release anyone waiting.
  if (!appsLoaded && apps.length > 0) {
    appsLoaded = true;
    const waiters = appsLoadedWaiters;
    appsLoadedWaiters = [];
    for (const wake of waiters) wake();
  }
  for (const listener of appsUpdatedListeners) listener(apps);
}

/** Take a pushed (or fetched) project list. */
export function applyProjects(next: ProjectInfo[]): void {
  for (const listener of projectsUpdatedListeners) listener(next);
}

/**
 * Report this browser's identity and active view to the shell (a ``client_state`` message).
 * Called on connect and on every view switch; ``previousViewId`` is set on a switch the user made
 * here so the shell records a ``view_switch`` in its client-activity log (a window following a
 * pushed ``active_view_changed`` reports without one). A no-op while the socket is down or before
 * a view has been chosen -- the next open re-reports.
 */
export function reportClientState(previousViewId?: string): void {
  const activeView = getActiveProjectId();
  if (ws === null || ws.readyState !== WebSocket.OPEN || !activeView) {
    console.info(
      `[si-ws] client_state not sent (readyState=${ws === null ? "no-socket" : ws.readyState} view=${JSON.stringify(activeView)})`,
    );
    return;
  }
  console.info(`[si-ws] sending client_state (client_id=${getClientId()} view=${activeView})`);
  ws.send(
    JSON.stringify({
      type: "client_state",
      client_id: getClientId(),
      device_kind: getDeviceKind(),
      active_view: activeView,
      previous_view: previousViewId ?? "",
    }),
  );
}

export function initInventory(): void {
  connect();
}

// ---------- Reads ----------

/** The apps a user can open from: every non-internal row, in registry order. */
export function getOpenableApps(): AppRecord[] {
  return apps.filter((app) => !app.internal);
}

export function getApp(name: string): AppRecord | undefined {
  return apps.find((app) => app.name === name);
}

/** The app and instance an address names, or null when nothing on the machine answers to it. */
export function findInstance(address: string): ResolvedInstance | null {
  const parsed = parseAddress(address);
  if (parsed === null) return null;
  const app = getApp(parsed.app);
  if (app === undefined) return null;
  const instance = app.instances.find((candidate) => candidate.key === parsed.key);
  if (instance === undefined) return null;
  return { app, instance, address };
}

/**
 * Whether the inventory says ``address`` is gone: its app is not registered, or the app's list
 * has arrived and does not carry it. False while the app's list is still pending, so a restored
 * tab is kept until the shell can actually say (an empty seed list is not an answer).
 */
export function isAddressUnlisted(address: string): boolean {
  const parsed = parseAddress(address);
  if (parsed === null) return true;
  const app = getApp(parsed.app);
  if (app === undefined) return true;
  if (!app.is_listed) return false;
  return !app.instances.some((candidate) => candidate.key === parsed.key);
}

/** Every instance of every openable app, in registry order and each app's own list order. */
export function listInstances(): ResolvedInstance[] {
  const listed: ResolvedInstance[] = [];
  for (const app of getOpenableApps()) {
    for (const instance of app.instances) {
      listed.push({ app, instance, address: addressFor(app.name, instance.key) });
    }
  }
  return listed;
}

/** Resolve to true once the app list has loaded, or to false after ``timeoutMs`` so a workspace
 *  that never reports any app still proceeds -- and the caller knows the inventory is not an
 *  answer yet. Share-critical URL construction awaits this so a restored tab never mounts an
 *  unroutable bare-name origin on a share. */
export function whenAppsLoaded(timeoutMs = 5000): Promise<boolean> {
  if (appsLoaded) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const settle = (isLoaded: boolean): void => {
      if (settled) return;
      settled = true;
      resolve(isLoaded);
    };
    appsLoadedWaiters.push(() => settle(true));
    setTimeout(() => settle(false), timeoutMs);
  });
}

// ---------- Listeners ----------

export function addAppsUpdatedListener(listener: AppsUpdatedListener): void {
  appsUpdatedListeners.push(listener);
}

export function removeAppsUpdatedListener(listener: AppsUpdatedListener): void {
  appsUpdatedListeners = appsUpdatedListeners.filter((l) => l !== listener);
}

export function addProjectsUpdatedListener(listener: ProjectsUpdatedListener): void {
  projectsUpdatedListeners.push(listener);
}

export function addLayoutOpListener(listener: LayoutOpListener): void {
  layoutOpListeners.push(listener);
}

export function addLayoutUpdatedListener(listener: LayoutUpdatedListener): void {
  layoutUpdatedListeners.push(listener);
}

export function addActiveViewChangedListener(listener: ActiveViewChangedListener): void {
  activeViewChangedListeners.push(listener);
}

export function addTabReboundListener(listener: TabReboundListener): void {
  tabReboundListeners.push(listener);
}

/** Forget every list and listener. Test-only. */
export function resetInventoryForTesting(): void {
  apps = [];
  appsLoaded = false;
  appsLoadedWaiters = [];
  appsUpdatedListeners = [];
  projectsUpdatedListeners = [];
  layoutOpListeners = [];
  layoutUpdatedListeners = [];
  activeViewChangedListeners = [];
  tabReboundListeners = [];
}
