/**
 * Provider accounts and the sign-in flows that create them.
 *
 * A "lane" is an AI provider reached through a particular harness -- what the UI calls a
 * provider. Several lanes can share a harness (Opencode Go and a raw API key both run on
 * Pi), so the chooser lists lanes, not harnesses.
 *
 * A sign-in has three possible shapes and the server tells us which, per method, so nothing
 * here has to know what a harness is:
 *
 *   url_then_code   here is a link; approve in the browser and paste the code back
 *   code_then_wait  here is a link and a one-time code; type it there and we wait
 *   paste           paste a key; no terminal involved
 *
 * Flows are single-flight on the server -- it holds one live sign-in at a time -- so this
 * keeps one flow's state and starting another abandons the first.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

export type FlowShape = "url_then_code" | "code_then_wait" | "paste";
export type FlowState = "pending" | "ok" | "failed";

export interface LaneMethod {
  id: string;
  label: string;
  description: string;
  shape: FlowShape;
  is_primary: boolean;
}

export interface KeyProvider {
  provider_id: string;
  display: string;
  env_var: string;
  hint: string;
}

export interface Lane {
  id: string;
  provider_name: string;
  subtitle: string;
  harness: string;
  methods: LaneMethod[];
  key_providers: KeyProvider[];
}

export interface ProviderAccount {
  id: string;
  lane: string;
  seq: number;
  display: string;
  harness: string;
  /** Already composed server-side ("Anthropic (Claude Code) 2") -- see accounts_endpoints. */
  label: string;
}

interface FlowStart {
  flow_id: string;
  shape: FlowShape;
  url: string | null;
  code: string | null;
}

interface FlowStatus {
  state: FlowState;
  detail: string | null;
  account_id: string | null;
}

let lanes: Lane[] = [];
let accounts: ProviderAccount[] = [];
let mru: string | null = null;
let lanesLoaded = false;

export function getLanes(): Lane[] {
  return lanes;
}

export function getAccounts(): ProviderAccount[] {
  return accounts;
}

export function getMruAccountId(): string | null {
  return mru;
}

export function areLanesLoaded(): boolean {
  return lanesLoaded;
}

export async function loadLanes(): Promise<void> {
  if (lanesLoaded) return;
  const body = await m.request<{ lanes: Lane[] }>({ method: "GET", url: apiUrl("/api/lanes") });
  lanes = body.lanes;
  lanesLoaded = true;
}

export async function loadAccounts(): Promise<void> {
  const body = await m.request<{ accounts: ProviderAccount[]; mru: string | null }>({
    method: "GET",
    url: apiUrl("/api/accounts"),
  });
  accounts = body.accounts;
  mru = body.mru;
}

export async function deleteAccount(accountId: string): Promise<void> {
  await m.request({ method: "DELETE", url: apiUrl(`/api/accounts/${accountId}`) });
  await loadAccounts();
}

/**
 * The live sign-in, if any. One at a time, matching the server.
 */
let flow: (FlowStart & { status: FlowStatus }) | null = null;
let pollTimer: number | null = null;

export function getFlow(): (FlowStart & { status: FlowStatus }) | null {
  return flow;
}

/**
 * Start a sign-in. Pass `accountId` to re-authenticate INTO an existing folder, which is
 * what lets every chat already bound to it recover rather than being orphaned.
 */
export async function startFlow(laneId: string, methodId: string, accountId?: string): Promise<void> {
  stopPolling();
  const started = await m.request<FlowStart>({
    method: "POST",
    url: apiUrl("/api/accounts"),
    body: { lane_id: laneId, method_id: methodId, account_id: accountId ?? null },
  });
  flow = { ...started, status: { state: "pending", detail: null, account_id: null } };
  // A paste flow is waiting on the user, not on a terminal, so there is nothing to poll.
  if (started.shape !== "paste") startPolling();
  m.redraw();
}

export async function submitCode(code: string): Promise<void> {
  if (flow === null) return;
  await advance({ code });
}

export async function submitKey(apiKey: string, keyProvider: string | null): Promise<void> {
  if (flow === null) return;
  await advance({ api_key: apiKey, key_provider: keyProvider });
}

async function advance(body: Record<string, unknown>): Promise<void> {
  if (flow === null) return;
  const status = await m.request<FlowStatus>({
    method: "POST",
    url: apiUrl(`/api/accounts/flow/${flow.flow_id}`),
    body,
  });
  await settle(status);
}

async function settle(status: FlowStatus): Promise<void> {
  if (flow === null) return;
  flow = { ...flow, status };
  if (status.state === "ok") {
    stopPolling();
    await loadAccounts();
  } else if (status.state === "failed") {
    stopPolling();
  }
  m.redraw();
}

function startPolling(): void {
  pollTimer = window.setInterval(() => {
    if (flow === null) {
      stopPolling();
      return;
    }
    m.request<FlowStatus>({ method: "GET", url: apiUrl(`/api/accounts/flow/${flow.flow_id}`) })
      // A poll that fails is not the flow failing -- the server may simply have moved on.
      // Keep the flow as it is and let the next tick or the deadline decide.
      .then((status) => settle(status))
      .catch(() => undefined);
  }, 2000);
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** Abandon the live flow. The server terminates the CLI and removes the folder. */
export function abortFlow(): void {
  stopPolling();
  if (flow !== null) {
    const id = flow.flow_id;
    flow = null;
    m.request({ method: "DELETE", url: apiUrl(`/api/accounts/flow/${id}`) }).catch(() => undefined);
  }
}

export function clearFlow(): void {
  stopPolling();
  flow = null;
}

/**
 * Which account the next chat launches on.
 *
 * Explicitly chosen wins; otherwise the most recently used, which the server bumps on
 * every launch -- so "start another one like the last" needs no click. Null means there
 * is nothing to launch on yet, and the New Chat button opens the chooser instead.
 */
let selectedAccountId: string | null = null;

export function getSelectedAccount(): ProviderAccount | null {
  const chosen = accounts.find((account) => account.id === selectedAccountId);
  if (chosen !== undefined) return chosen;
  const recent = accounts.find((account) => account.id === mru);
  return recent ?? accounts[0] ?? null;
}

export function selectAccount(accountId: string): void {
  selectedAccountId = accountId;
  m.redraw();
}

/**
 * Whether the chooser is showing. One app-level modal, like the login modal it replaces:
 * accounts are mind-global, so there is nothing per-chat about picking one.
 */
let chooserOpen = false;

export function isProviderChooserOpen(): boolean {
  return chooserOpen;
}

export function openProviderChooser(): void {
  if (chooserOpen) return;
  chooserOpen = true;
  m.redraw();
}

export function closeProviderChooser(): void {
  if (!chooserOpen) return;
  chooserOpen = false;
  m.redraw();
}
