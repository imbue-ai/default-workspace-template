/**
 * The shell's view of the machine: every registered app with its instances, as the shell's
 * WebSocket pushes it (workspace app model, contracts.md section 8), plus the projects, the
 * agent-driven layout ops, and the tab rebinds that ride the same socket.
 *
 * This is the one socket the shell holds. On connect the server sends ``apps_updated`` and
 * ``projects_updated``; the shell answers with its ``client_state`` (which client it is, on
 * which device, looking at which view) and re-sends it on every view switch.
 *
 * Nothing here knows what any app is: a chat, a terminal and a browser are apps with instances
 * like any other, and the surfaces that draw them read the app's own display name, icon,
 * actions and instance titles off these records.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { deriveServiceOrigin, workspaceHostCoordinate } from "../origin";
import { ReconnectBackoff } from "./backoff";
import { getActiveProjectId, getClientId, getDeviceKind } from "./ClientIdentity";
import { parseJsonMessage } from "./ws-json";

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
}

export interface AppAction {
  id: string;
  label: string;
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

// The names of the layout ops the agent-facing ``system/scripts/layout.py`` helper can emit.
export type LayoutOpName =
  | "open"
  | "focus"
  | "split"
  | "close"
  | "move"
  | "maximize"
  | "restore"
  | "refresh"
  | "reload_system_interface";

export interface LayoutOpEvent {
  op: LayoutOpName;
  args: Record<string, unknown>;
  /** ``MNGR_AGENT_ID`` of the agent that invoked the helper, "" when unset. */
  requesterAgentId: string;
}

/** ``layout.py load <view>``: an agent switching a client onto a view. */
export interface LoadViewEvent {
  viewId: string;
  displayName: string;
  targetClientId: string | null;
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
  | { type: "layout_op"; op: LayoutOpName; args: Record<string, unknown>; requester_agent_id?: string }
  | { type: "load_layout"; view_id: string; display_name: string; target_client_id: string | null }
  | { type: "tab_rebound"; client_id: string; view_id: string; tab_id: string; address: string };

export type AppsUpdatedListener = (apps: AppRecord[]) => void;
export type ProjectsUpdatedListener = (projects: ProjectInfo[]) => void;
export type LayoutOpListener = (event: LayoutOpEvent) => void;
export type LoadViewListener = (event: LoadViewEvent) => void;
export type TabReboundListener = (event: TabReboundEvent) => void;

const ADDRESS_SCHEME = "app:";
const ADDRESS_INSTANCE_PARAMETER = "?instance=";

// ---------- Addresses (pure) ----------

/** The address of ``app``'s instance ``key`` (``app:<name>?instance=<key>``), or of the app itself for "" (``app:<name>``). */
export function addressFor(appName: string, key: string): string {
  return key === "" ? `${ADDRESS_SCHEME}${appName}` : `${ADDRESS_SCHEME}${appName}${ADDRESS_INSTANCE_PARAMETER}${key}`;
}

/** The app and key an address names (``key`` is "" for the bare form), or null for anything else. */
export function parseAddress(address: string): { app: string; key: string } | null {
  if (!address.startsWith(ADDRESS_SCHEME)) return null;
  const body = address.substring(ADDRESS_SCHEME.length);
  const separator = body.indexOf("?");
  if (separator === -1) return body === "" ? null : { app: body, key: "" };
  const app = body.substring(0, separator);
  const remainder = body.substring(separator);
  if (app === "" || !remainder.startsWith(ADDRESS_INSTANCE_PARAMETER)) return null;
  const key = remainder.substring(ADDRESS_INSTANCE_PARAMETER.length);
  return key === "" ? null : { app, key };
}

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

/** Whether the workspace can stop and start this app: supervised, and not critical to the workspace. */
export function isAppStoppable(app: AppRecord): boolean {
  return app.program !== "" && !app.critical;
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
let loadViewListeners: LoadViewListener[] = [];
let tabReboundListeners: TabReboundListener[] = [];
let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

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
      for (const listener of layoutOpListeners) {
        listener({ op: event.op, args: event.args, requesterAgentId: event.requester_agent_id ?? "" });
      }
      break;
    case "load_layout":
      for (const listener of loadViewListeners) {
        listener({ viewId: event.view_id, displayName: event.display_name, targetClientId: event.target_client_id });
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
 * Called on connect and on every view switch; ``previousViewId`` is set on a switch so the
 * shell records a ``view_switch`` in its client-activity log. A no-op while the socket is down
 * or before a view has been chosen -- the next open re-reports.
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

export function addLoadViewListener(listener: LoadViewListener): void {
  loadViewListeners.push(listener);
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
  loadViewListeners = [];
  tabReboundListeners = [];
}
