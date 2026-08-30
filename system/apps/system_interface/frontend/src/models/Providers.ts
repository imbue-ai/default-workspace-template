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
  /** Empty unless the provider has to be signed up for before a key exists. */
  signup_url: string;
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
  /** The harness's display name ("Claude Code"), for the header's "Runs on" line. */
  harness_label: string;
  methods: LaneMethod[];
  key_providers: KeyProvider[];
}

export interface ProviderAccount {
  id: string;
  lane: string;
  /** Which harness runs this account -- what the combo card greys a row by. */
  harness: string;
  /** The provider noun on its own ("Groq"), and the harness's display name ("Pi"). The card
   *  renders them at different sizes on one row; `label` is the same thing composed. */
  provider: string;
  harness_label: string;
  seq: number;
  /** The name the user gave this account, or "" for the provider's own. `provider` already
   *  reflects it -- this is here so the rename field can tell "never renamed" from "renamed
   *  to what it was called anyway". */
  name: string;
  /** Already composed server-side ("Anthropic 2 (Claude Code)") -- see accounts_endpoints. */
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
// Whether the account list has been fetched even once. Distinct from "it is empty": both read
// as zero accounts, and one of them means "ask the user to sign in".
let accountsLoaded = false;

export function getLanes(): Lane[] {
  return lanes;
}

export function getAccounts(): ProviderAccount[] {
  return accounts;
}

/** The account a chat is running on, from its own `account` label.
 *
 * Null when the chat predates accounts, or when the account has since been deleted -- the
 * label is a dangling id in that case, which is the accepted cost of delete-and-re-add.
 */
export function accountForAgent(accountId: string | undefined): ProviderAccount | null {
  if (!accountId) return null;
  return accounts.find((candidate) => candidate.id === accountId) ?? null;
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
  accountsLoaded = true;
}

/** Whether `getAccounts()` has an answer yet. Anything that treats "no accounts" as "sign in
 *  first" has to ask this too, or it diverts the user on a workspace that has providers. */
export function areAccountsLoaded(): boolean {
  return accountsLoaded;
}

/** Name an account, or pass "" to go back to the provider's own name.
 *
 * Display only: nothing keys off the name, so this cannot strand a chat. */
export async function renameAccount(accountId: string, name: string): Promise<void> {
  await m.request({ method: "PATCH", url: apiUrl(`/api/accounts/${accountId}`), body: { name } });
  await loadAccounts();
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
/** Bumped by every `startFlow`, so a response that has been superseded can tell. */
let startGeneration = 0;

export async function startFlow(laneId: string, methodId: string, accountId?: string): Promise<void> {
  // Which start this is. The server is single-flight and displaces the older flow, so if two
  // POSTs resolve out of order the LATER response is the live one -- and without this the
  // earlier one lands last and leaves `flow` naming a session the server has already forgotten.
  // Every poll then 404s and the modal says the sign-in was replaced, while a live flow exists.
  startGeneration += 1;
  const attempt = startGeneration;
  const started = await m.request<FlowStart>({
    method: "POST",
    url: apiUrl("/api/accounts"),
    body: { lane_id: laneId, method_id: methodId, account_id: accountId ?? null },
  });
  if (attempt !== startGeneration) return;
  flow = { ...started, status: { state: "pending", detail: null, account_id: null } };
  // Stopped HERE rather than before the await: two overlapping sign-ins both reached the await
  // with nothing yet to stop, and both then started a poller. The first interval was left with
  // no reference to it, GETting a flow id the server had already forgotten every two seconds
  // for the life of the page.
  stopPolling();
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
  const flowId = flow.flow_id;
  const status = await m.request<FlowStatus>({
    method: "POST",
    url: apiUrl(`/api/accounts/flow/${flowId}`),
    body,
  });
  await settle(status, flowId);
}

async function settle(status: FlowStatus, flowId: string): Promise<void> {
  // A response that outlived its flow must not be applied to the next one: starting a second
  // sign-in displaces the first, and an in-flight poll or submit from the first would
  // otherwise stamp its state -- including "failed" -- onto a flow that is doing fine.
  if (flow === null || flow.flow_id !== flowId) return;
  // A flow that has already settled stays settled. `ok` and `failed` are terminal, and the
  // poller is stopped on reaching one -- but a poll already in flight when that happened still
  // resolves afterwards, and a `pending` from it would put a finished sign-in back on the
  // spinner with nothing left running to correct it.
  if (flow.status.state === "ok" || flow.status.state === "failed") return;
  flow = { ...flow, status };
  if (status.state === "ok") {
    stopPolling();
    // The account the user just created is the one their next chat should use. Without
    // this, someone who picked an account earlier and then added a provider gets the old
    // one, silently.
    if (status.account_id !== null) selectedAccountId = status.account_id;
    await loadAccounts();
    // After `loadAccounts`, so a callback that opens a chat sees the account it will bind to.
    if (status.account_id !== null && chooserOnSignedIn !== null) {
      const run = chooserOnSignedIn;
      chooserOnSignedIn = null;
      run(status.account_id);
    }
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
    const flowId = flow.flow_id;
    m.request<FlowStatus>({ method: "GET", url: apiUrl(`/api/accounts/flow/${flowId}`) })
      .then((status) => settle(status, flowId))
      .catch((error: { code?: number }) => {
        // 404 means the server no longer has this flow -- another sign-in displaced it, or
        // it was torn down. No later tick will say anything different, so swallowing it
        // leaves this screen spinning forever.
        if (error.code === 404 && flow !== null && flow.flow_id === flowId) {
          void settle(
            { state: "failed", detail: "That sign-in was replaced by a newer one.", account_id: null },
            flowId,
          );
          return;
        }
        // Anything else is this poll failing, not the flow. Let the next tick decide.
      });
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
// Which account the chooser should open ON, when it is being opened to fix a specific one
// rather than to add a provider. Module state rather than an attribute because every caller
// reaches the modal through `openProviderChooser`, and threading an argument through a
// 780-line component for two callers is the worse trade.
let chooserAccountId: string | null = null;
// What to do once a sign-in succeeds. Signing in from a NEW-TAB surface means the user was
// trying to start a chat and had to authenticate on the way, so the chat opens on the account
// they just added. Signing in from inside a chat means they were adding a provider for later
// and should not be moved. The caller knows which it is; nothing here can tell.
let chooserOnSignedIn: ((accountId: string) => void) | null = null;

export function isProviderChooserOpen(): boolean {
  return chooserOpen;
}

export interface ProviderChooserIntent {
  /** Re-authenticate THIS account rather than add a provider. */
  accountId?: string;
  /** Run once a sign-in succeeds, with the account it produced. */
  onSignedIn?: (accountId: string) => void;
}

/** Open the chooser, optionally saying why it was opened. */
export function openProviderChooser(intent: ProviderChooserIntent = {}): void {
  if (chooserOpen) return;
  chooserOpen = true;
  chooserAccountId = intent.accountId ?? null;
  chooserOnSignedIn = intent.onSignedIn ?? null;
  m.redraw();
}

/** The account the chooser was opened on, or null when it was opened to add a provider.
 *  Read once by the modal at init; cleared so a later open starts from the lane list. */
export function takeChooserAccountId(): string | null {
  const accountId = chooserAccountId;
  chooserAccountId = null;
  return accountId;
}

export function closeProviderChooser(): void {
  if (!chooserOpen) return;
  chooserOpen = false;
  // Cleared on close as well as on open: a chooser dismissed without signing in must not
  // leave a callback armed for whoever opens it next.
  chooserOnSignedIn = null;
  m.redraw();
}
