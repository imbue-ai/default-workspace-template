/**
 * The store's state as the app serves it at ``GET /api/store`` (see ``src/app_store/views.py``), polled: every
 * couple of seconds while an install or uninstall runs, so its progress moves, and every few seconds otherwise, so
 * the room and the workspace's apps stay current. Install and uninstall are POSTs that queue a job; their refusal
 * (the app won't fit, it is busy) comes back as a plain sentence the page shows.
 *
 * The pure helpers below (grouping, the lookups the views need) are exported so they can be tested without a socket.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export type RoomVerdict = "FITS" | "TIGHT" | "WONT_FIT";
export type InstallStatus =
  "QUEUED" | "DOWNLOADING" | "CONFIGURING" | "STARTING" | "INSTALLED" | "FAILED" | "UNINSTALLING";
export type JobKind = "INSTALL" | "REPAIR" | "UNINSTALL" | "UPDATE";
/** How a newer version of an installed app is found: with the workspace, on PyPI or npm, or by a chat. */
export type UpdateKind = "CATALOG" | "REGISTRY" | "GITHUB";
export type AppOrigin = "BUILT_IN" | "MADE_HERE" | "STORE";

export interface WorkspaceRoom {
  disk_free_mb: number;
  memory_available_mb: number;
}

export interface AppCategory {
  key: string;
  title: string;
}

/** What an app the user added was added with. */
export interface AddedWith {
  registry: string;
  package: string;
  version: string;
  command: string[];
  python: string | null;
  registry_url: string;
  package_count: number | null;
  /** For a GitHub repository: the chat that worked out how to install it; empty otherwise. */
  chat_id: string;
}

export interface StoreApp {
  slug: string;
  /** Whether the user added it (nobody reviewed it) rather than the store shipping it. */
  is_added: boolean;
  added_with: AddedWith | null;
  /** The keys of the categories it belongs to. */
  tags: string[];
  title: string;
  description: string;
  about: string;
  author: string;
  license: string;
  homepage_url: string;
  source_url: string;
  version: string;
  source_label: string;
  package_spec: string;
  icon_url: string;
  disk_mb: number | null;
  memory_idle_mb: number | null;
  /** What it holds while one person uses it; the fit is checked against this. */
  memory_in_use_mb: number | null;
  /** What a built-from-GitHub app needs while it builds; null for one that is not built. */
  build_disk_mb: number | null;
  build_memory_mb: number | null;
  /** How the figures were worked out, in plain words. */
  resources_basis: string;
  /** The most its processes held in the first minutes of a run; null until it has run. */
  peak_memory_mb: number | null;
  /** What the installed app's processes use right now; null when not installed or not running. */
  memory_now_mb: number | null;
  verdict: RoomVerdict;
  blocked_reason: string;
  status: InstallStatus | null;
  job: JobKind | null;
  failure_reason: string;
  measured_disk_mb: number | null;
  app_url: string | null;
  has_log: boolean;
  /** The version installed now; null when it is not installed. */
  installed_version: string | null;
  /** How a newer version is found; null when it is not installed. */
  update_kind: UpdateKind | null;
  /** A newer version ready to install now (a shipped app's); null when none is known. */
  update_version: string | null;
}

export interface WorkspaceApp {
  name: string;
  display_name: string;
  description: string;
  icon_svg: string;
  url: string;
  origin: AppOrigin;
}

/** An app a chat worked out how to install from GitHub, waiting for the person to confirm it. */
export interface PendingRepoAdd {
  id: string;
  slug: string;
  title: string;
  description: string;
  about: string;
  /** The repository's owner/name. */
  repository: string;
  repository_url: string;
  commit: string;
  commit_url: string;
  source_label: string;
  /** The install's commands, in order, exactly as they will run. */
  install_commands: string[];
  /** The command that starts it, with a sample port. */
  start_command: string;
  disk_mb: number | null;
  memory_in_use_mb: number | null;
  build_disk_mb: number | null;
  build_memory_mb: number | null;
  /** How it was worked out and measured, in plain words. */
  basis: string;
  verdict: RoomVerdict;
  blocked_reason: string;
  chat_id: string;
  submitted_at: string;
  /** For an update: the installed app this newer version replaces; empty for an add. */
  replaces: string;
  /** For an update: the version installed now. */
  installed_version: string;
  /** The version it installs. */
  version: string;
  /** The trial's screenshot of the app running, inside a frame; null when it has none. */
  screenshot_url: string | null;
}

export interface StoreSnapshot {
  room: WorkspaceRoom;
  categories: AppCategory[];
  apps: StoreApp[];
  workspace_apps: WorkspaceApp[];
  busy_with: string | null;
  /** Apps from GitHub waiting for confirmation, oldest first. */
  pending_repo_adds: PendingRepoAdd[];
}

export type StoreState = { kind: "loading" } | { kind: "loaded"; snapshot: StoreSnapshot } | { kind: "failed" };

const STORE_PATH = "/api/store";
const BUSY_POLL_MS = 1500;
const IDLE_POLL_MS = 8000;

/** The statuses in which a job is queued or running for an app. */
const WORKING_STATUSES: readonly InstallStatus[] = [
  "QUEUED",
  "DOWNLOADING",
  "CONFIGURING",
  "STARTING",
  "UNINSTALLING",
];

// pure helpers

export function isWorking(app: StoreApp): boolean {
  return app.status !== null && WORKING_STATUSES.includes(app.status);
}

/** The catalog apps still to offer: everything not installed (an app being installed, or whose install failed,
 *  stays here with its progress or its failure). */
export function availableApps(snapshot: StoreSnapshot): StoreApp[] {
  return snapshot.apps.filter((app) => app.status !== "INSTALLED" && app.status !== "UNINSTALLING");
}

/** The catalog entry behind an app the store installed, so its tile can offer Uninstall and show its cost. */
export function storeAppFor(snapshot: StoreSnapshot, workspaceApp: WorkspaceApp): StoreApp | null {
  if (workspaceApp.origin !== "STORE") return null;
  return snapshot.apps.find((app) => app.slug === workspaceApp.name) ?? null;
}

/** An installed store app that is being uninstalled no longer has a registry row once the job removes it, but the
 *  page shows it until then. */
export function uninstallingApps(snapshot: StoreSnapshot): StoreApp[] {
  const listed = new Set(snapshot.workspace_apps.map((app) => app.name));
  return snapshot.apps.filter((app) => app.status === "UNINSTALLING" && !listed.has(app.slug));
}

/** Whether a store app is in the chosen category; every app is when none is chosen ("All apps"). */
export function isInCategory(app: StoreApp, category: string | null): boolean {
  return category === null || app.tags.includes(category);
}

export function titleOf(snapshot: StoreSnapshot, slug: string | null): string | null {
  if (slug === null) return null;
  return snapshot.apps.find((app) => app.slug === slug)?.title ?? null;
}

export function iconDataUrl(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

/** Whether the store is working on something, so the page polls fast. */
export function isBusy(snapshot: StoreSnapshot): boolean {
  return snapshot.busy_with !== null || snapshot.apps.some(isWorking);
}

// the poll

let state: StoreState = { kind: "loading" };
let pollTimer: ReturnType<typeof setTimeout> | null = null;
// Each poll's number: a request that queues a job polls at once, so polls can overlap, and only the newest one's
// answer is kept and schedules the next (an older answer arriving later would undo what the newer one showed).
let latestPoll = 0;

export function getStoreState(): StoreState {
  return state;
}

/** Start polling the store (once per page load). */
export function startStorePolling(): void {
  if (pollTimer !== null) return;
  void pollOnce();
}

/** Fetch now, then keep polling at the pace the state calls for. */
export async function pollOnce(): Promise<void> {
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  latestPoll += 1;
  const thisPoll = latestPoll;
  const fetched = await fetchStore();
  if (thisPoll !== latestPoll) return;
  state = fetched;
  m.redraw();
  const delay = state.kind === "loaded" && isBusy(state.snapshot) ? BUSY_POLL_MS : IDLE_POLL_MS;
  pollTimer = setTimeout(() => void pollOnce(), delay);
}

async function fetchStore(): Promise<StoreState> {
  try {
    const response = await fetch(apiUrl(STORE_PATH));
    if (!response.ok) {
      console.warn(`[app-store] could not load the store: HTTP ${response.status}`);
      return state.kind === "loaded" ? state : { kind: "failed" };
    }
    return { kind: "loaded", snapshot: (await response.json()) as StoreSnapshot };
  } catch (e) {
    console.warn("[app-store] could not load the store", e);
    return state.kind === "loaded" ? state : { kind: "failed" };
  }
}

// the requests

const UNREACHABLE = "The App Store could not be reached. Try again in a moment.";

async function requestJson(path: string, method: string, body: unknown): Promise<Response> {
  return fetch(apiUrl(path), {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function refusalOf(response: Response): Promise<string> {
  const answer = (await response.json().catch(() => ({}))) as { detail?: string };
  return answer.detail ?? `The store answered ${response.status}.`;
}

/** Send a request that changes the store (queues a job, adds or drops an entry); resolves to null when it was
 *  accepted, and polls at once so the page shows the change, else to the plain reason it was refused. */
async function sendChange(path: string, method: "POST" | "DELETE", body: unknown): Promise<string | null> {
  try {
    const response = await requestJson(path, method, body);
    if (response.ok) {
      void pollOnce();
      return null;
    }
    return await refusalOf(response);
  } catch {
    return UNREACHABLE;
  }
}

export function requestInstall(slug: string): Promise<string | null> {
  return sendChange(`/api/apps/${encodeURIComponent(slug)}/install`, "POST", undefined);
}

/** Queue an update to ``version`` (empty for the newest); null when queued, else why not. */
export function requestUpdate(slug: string, version: string): Promise<string | null> {
  return sendChange(`/api/apps/${encodeURIComponent(slug)}/update`, "POST", { version });
}

export type NewestVersionAnswer =
  | { kind: "checked"; installedVersion: string; newestVersion: string; isNewer: boolean }
  | { kind: "refused"; reason: string };

/** Look up the newest version of an installed app (with the workspace, or on PyPI or npm). */
export async function fetchNewestVersion(slug: string): Promise<NewestVersionAnswer> {
  try {
    const response = await fetch(apiUrl(`/api/apps/${encodeURIComponent(slug)}/newest-version`));
    const answer = (await response.json().catch(() => ({}))) as {
      installed_version?: string;
      newest_version?: string;
      is_newer?: boolean;
      detail?: string;
    };
    if (!response.ok) return { kind: "refused", reason: answer.detail ?? `The store answered ${response.status}.` };
    return {
      kind: "checked",
      installedVersion: answer.installed_version ?? "",
      newestVersion: answer.newest_version ?? "",
      isNewer: answer.is_newer === true,
    };
  } catch {
    return { kind: "refused", reason: UNREACHABLE };
  }
}

/** The message that opens a chat checking an app added from GitHub for a newer version. */
export function githubUpdateMessage(app: StoreApp): string {
  return `/add-repo-to-store ${app.homepage_url} --update ${app.slug}`;
}

export function requestUninstall(slug: string, isDeletingData: boolean): Promise<string | null> {
  return sendChange(`/api/apps/${encodeURIComponent(slug)}/uninstall`, "POST", { is_deleting_data: isDeletingData });
}

export function installLogUrl(slug: string): string {
  return apiUrl(`/api/apps/${encodeURIComponent(slug)}/log`);
}

export type PackageSource = "pypi" | "npm";

/** A settings file written into an app's own folder before its first start. */
export interface SetupFile {
  /** Relative to the app's folder, e.g. config.yaml. */
  path: string;
  content: string;
}

/** What the "Add app" dialog sends. */
export interface AddAppRequest {
  source: PackageSource;
  package: string;
  version: string;
  display_name: string;
  command: string;
  python: string;
  setup_files: SetupFile[];
}

/** What "Check" answers. */
export interface CheckResult {
  slug: string;
  title: string;
  source: PackageSource;
  registry: string;
  package: string;
  version: string;
  python: string | null;
  registry_url: string;
  summary: string;
  license: string;
  command: string[];
  command_preview: string[];
  setup_files: SetupFile[];
  package_count: number | null;
  disk_mb: number | null;
  disk_basis: string;
  verdict: RoomVerdict;
  blocked_reason: string;
}

export type CheckAnswer = { kind: "checked"; result: CheckResult } | { kind: "refused"; reason: string };

/** Check an app to add, installing nothing. */
export async function checkAddedApp(request: AddAppRequest): Promise<CheckAnswer> {
  try {
    const response = await requestJson("/api/added-apps/check", "POST", request);
    if (response.ok) return { kind: "checked", result: (await response.json()) as CheckResult };
    return { kind: "refused", reason: await refusalOf(response) };
  } catch {
    return { kind: "refused", reason: UNREACHABLE };
  }
}

/** Add the app and queue its install; null when queued, else why not. */
export function addApp(request: AddAppRequest): Promise<string | null> {
  return sendChange("/api/added-apps", "POST", request);
}

export type FinderStatus = "WORKING" | "FOUND" | "NOT_AN_APP" | "NEEDS_SETUP" | "FAILED" | "CANCELLED";

/** One command the store tried while working out how to start an app. */
export interface FinderAttempt {
  command: string;
  outcome: string;
}

/** A job working out an added app's start command (AI writes it; the store test-launches it). */
export interface FinderJob {
  job_id: string;
  status: FinderStatus;
  /** What it is doing now, in plain words; empty once finished. */
  progress: string;
  package: string;
  version: string;
  /** The command found; empty unless FOUND. */
  command: string;
  /** The settings files it starts with; empty unless FOUND with files. */
  setup_files: SetupFile[];
  /** What the app is for, what the package is instead, what it needs, or what went wrong. */
  explanation: string;
  attempts: FinderAttempt[];
  has_screenshot: boolean;
  started_at: string;
}

export type FindAnswer = { kind: "started"; job: FinderJob } | { kind: "refused"; reason: string };

const FIND_COMMAND_PATH = "/api/added-apps/find-command";

/** Start working out a request's start command (its command field is ignored). */
export async function startFindCommand(request: AddAppRequest): Promise<FindAnswer> {
  try {
    const response = await requestJson(FIND_COMMAND_PATH, "POST", { ...request, command: "", setup_files: [] });
    if (response.ok) return { kind: "started", job: ((await response.json()) as { job: FinderJob }).job };
    return { kind: "refused", reason: await refusalOf(response) };
  } catch {
    return { kind: "refused", reason: UNREACHABLE };
  }
}

/** Where a job stands now; null when it could not be read this time. */
export async function fetchFinderJob(jobId: string): Promise<FinderJob | null> {
  try {
    const response = await fetch(apiUrl(`${FIND_COMMAND_PATH}/${encodeURIComponent(jobId)}`));
    if (!response.ok) return null;
    return ((await response.json()) as { job: FinderJob }).job;
  } catch {
    return null;
  }
}

/** Stop a job; it cleans up what it downloaded. */
export async function cancelFindCommand(jobId: string): Promise<void> {
  try {
    await requestJson(`${FIND_COMMAND_PATH}/${encodeURIComponent(jobId)}/cancel`, "POST", undefined);
  } catch {
    // A cancel that does not arrive leaves the job to finish on its own; it still cleans up after itself.
  }
}

export function finderScreenshotUrl(jobId: string): string {
  return apiUrl(`${FIND_COMMAND_PATH}/${encodeURIComponent(jobId)}/screenshot.png`);
}

export function finderLogUrl(jobId: string): string {
  return apiUrl(`${FIND_COMMAND_PATH}/${encodeURIComponent(jobId)}/log`);
}

const REPO_ADDS_PATH = "/api/repo-adds";

/** Add an app from GitHub the person confirmed, and queue its install; null when queued, else why not. */
export function confirmRepoAdd(pendingId: string): Promise<string | null> {
  return sendChange(`${REPO_ADDS_PATH}/${encodeURIComponent(pendingId)}/confirm`, "POST", undefined);
}

/** Drop an app from GitHub that was waiting for confirmation; null when done, else why not. */
export function discardRepoAdd(pendingId: string): Promise<string | null> {
  return sendChange(`${REPO_ADDS_PATH}/${encodeURIComponent(pendingId)}`, "DELETE", undefined);
}

/** Take an added app out of the store; null when done, else why not. */
export function removeAddedApp(slug: string): Promise<string | null> {
  return sendChange(`/api/added-apps/${encodeURIComponent(slug)}`, "DELETE", undefined);
}

/** Whether an added app can be taken out of the store now: it is not installed or being worked on. */
export function isRemovable(app: StoreApp): boolean {
  return app.is_added && (app.status === null || app.status === "FAILED");
}

/**
 * The words of a start command, split the way the store splits it (POSIX shell rules for quotes and backslashes,
 * nothing else: no variables, no globbing), so the dialog can show what will run before it is checked. Null when the
 * quotes do not close.
 */
export function splitCommand(command: string): string[] | null {
  const words: string[] = [];
  let word = "";
  let inWord = false;
  let quote: "'" | '"' | null = null;
  for (let index = 0; index < command.length; index += 1) {
    const character = command[index];
    if (quote === "'") {
      if (character === "'") quote = null;
      else word += character;
    } else if (quote === '"') {
      if (character === '"') quote = null;
      else if (character === "\\" && index + 1 < command.length && '"\\$`'.includes(command[index + 1])) {
        index += 1;
        word += command[index];
      } else word += character;
    } else if (character === "'" || character === '"') {
      quote = character;
      inWord = true;
    } else if (character === "\\") {
      if (index + 1 < command.length) {
        index += 1;
        word += command[index];
      }
      inWord = true;
    } else if (/\s/.test(character)) {
      if (inWord) words.push(word);
      word = "";
      inWord = false;
    } else {
      word += character;
      inWord = true;
    }
  }
  if (quote !== null) return null;
  if (inWord) words.push(word);
  return words;
}

/** A template's size in KB, or null when it is not estimated. */
export async function fetchTemplateSizeKb(slug: string): Promise<number | null> {
  try {
    const response = await fetch(apiUrl(`/api/templates/${encodeURIComponent(slug)}/size`));
    if (!response.ok) return null;
    return ((await response.json()) as { size_kb: number | null }).size_kb;
  } catch {
    return null;
  }
}

/** For tests: forget the polled state. */
export function resetStoreStateForTests(): void {
  if (pollTimer !== null) clearTimeout(pollTimer);
  pollTimer = null;
  latestPoll += 1;
  state = { kind: "loading" };
}
