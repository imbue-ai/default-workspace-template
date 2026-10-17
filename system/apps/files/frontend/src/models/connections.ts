/**
 * What the user has shared with this workspace from their computers (the Files server's `/__files/api/connections`,
 * read from the latchkey gateway), and the ways each share reaches the workspace: synced both ways, synced one way
 * from the computer, or read live from the computer with no copy here.
 */

import { DufsError } from "./dufs";
import { formatModified, formatSize } from "./format";
import { nameOf } from "./paths";

export const CONNECTIONS_API = "/__files/api/connections";
/** Where a share with no copy here is browsed: the Files server answers it from the computer, live. */
export const REMOTE_PREFIX = "/__remote/";

export interface Device {
  id: string;
  name: string;
}

export interface Share {
  device: string;
  /** Where it is on the computer. */
  path: string;
  access: "read" | "write";
  kind: "folder" | "file" | "unknown";
  is_reachable: boolean;
  /** Where the synced copy is in the workspace, or null when there is none. */
  synced_copy: string | null;
  /** How Imbue Studio says the copy stands; null for a share with no copy, or a copy whose computer didn't say. */
  sync: FolderSync | null;
}

/** A sync as Imbue Studio on the computer reports it. */
export interface FolderSync {
  state:
    | "STARTING"
    | "RESTARTING"
    | "SYNCING"
    | "SYNCED"
    | "DEACTIVATING"
    | "DISCARDING"
    | "STOPPED"
    | "UNKNOWN"
    | "FAILED";
  /** Why it failed, in Imbue Studio's words. */
  message: string | null;
  bytes_done: number | null;
  bytes_total: number | null;
  /** Whether the copy has once held everything the computer did. */
  has_finished_first_copy: boolean;
  /** When a pass last finished (ISO 8601). */
  last_synced_at: string | null;
}

/** How a synced copy stands, as Files says it. */
export type SyncStatus =
  | { kind: "copying"; done: number | null; total: number | null }
  | { kind: "syncing"; done: number | null; total: number | null }
  | { kind: "current"; lastSyncedAt: string | null }
  | { kind: "failed"; message: string | null }
  | { kind: "paused" }
  | { kind: "unknown" };

/** How a share's copy stands, or null for a share with no copy. */
export function syncStatusOf(share: Share): SyncStatus | null {
  if (share.synced_copy === null) return null;
  const sync = share.sync;
  if (sync === null) return { kind: "unknown" };
  switch (sync.state) {
    case "FAILED":
      return { kind: "failed", message: sync.message };
    case "STOPPED":
    case "UNKNOWN":
    case "DEACTIVATING":
    case "DISCARDING":
      return { kind: "paused" };
    case "SYNCED":
      if (sync.has_finished_first_copy) return { kind: "current", lastSyncedAt: sync.last_synced_at };
      break;
    default:
      break;
  }
  const progress = { done: sync.bytes_done, total: sync.bytes_total };
  return sync.has_finished_first_copy ? { kind: "syncing", ...progress } : { kind: "copying", ...progress };
}

/** "10 MB of 40 MB", or "" when the sync didn't say how far along it is. */
function progressOf(done: number | null, total: number | null): string {
  if (done === null || total === null || total <= 0) return "";
  return `${formatSize(done)} of ${formatSize(total)}`;
}

export interface Connections {
  devices: Device[];
  shares: Share[];
}

/** How a share reaches the workspace. */
export type ShareMode = "two-way" | "one-way" | "live";

export function modeOf(share: Share): ShareMode {
  if (share.synced_copy === null) return "live";
  return share.access === "write" ? "two-way" : "one-way";
}

/** The badge a share wears on its icon: a chain when it is read live, arrows when a copy of it is kept here. */
export type ShareBadge = "linked" | "synced";

export function shareBadgeOf(share: Share): ShareBadge {
  return share.synced_copy === null ? "linked" : "synced";
}

/** The share whose own folder or file is at `path` (not one it holds), if any. */
export function shareRootAt(connections: Connections | null, path: string): Share | undefined {
  return connections?.shares.find((share) => targetOf(share) === path);
}

/** The name of the computer a share is on, as people say it. */
export function deviceNameOf(connections: Connections, device: string): string {
  const name = connections.devices.find((candidate) => candidate.id === device)?.name;
  return name === undefined ? "your computer" : shortDeviceName(name);
}

/** Where opening a share goes: its synced copy, or the live view of it on its computer. */
export function targetOf(share: Share): string {
  if (share.synced_copy !== null) return share.synced_copy;
  const live = `${REMOTE_PREFIX}${share.device}${share.path}`;
  return share.kind === "file" ? live : `${live.replace(/\/+$/, "")}/`;
}

/** Whether `path` is read live from a computer, whose file server can neither rename nor zip a folder. */
export function isLiveShared(path: string): boolean {
  return path.startsWith(REMOTE_PREFIX);
}

export function labelOf(share: Share): string {
  return nameOf(share.path) || share.path;
}

/** A computer's name as people say it, without the ``.local`` macOS adds on the network. */
export function shortDeviceName(deviceName: string): string {
  return deviceName.replace(/\.local$/, "");
}

/** The hover text of a share: where it lives and how it reaches the workspace, whether it is read-only, and how it
 *  stands, never its own name. */
export function describeShare(share: Share, device: string): string {
  const where = {
    "two-way": `Synced with ${device}.`,
    "one-way": `Synced from ${device}.`,
    live: `On ${device}.`,
  }[modeOf(share)];
  const access = share.access === "read" ? "Read-only." : "";
  return [where, access, statusOf(share)].filter((part) => part !== "").join(" ");
}

function statusOf(share: Share): string {
  const status = syncStatusOf(share);
  if (status === null) return share.is_reachable ? "" : "Not answering right now.";
  switch (status.kind) {
    case "copying":
      return joined("Copying the first time", progressOf(status.done, status.total));
    case "syncing":
      return joined("Syncing", progressOf(status.done, status.total));
    case "current":
      return status.lastSyncedAt === null
        ? "Up to date."
        : `Up to date, last synced ${lowerDay(formatModified(Date.parse(status.lastSyncedAt)))}.`;
    case "failed":
      return status.message === null ? "Sync stopped." : `Sync stopped: ${status.message}`;
    case "paused":
      return "Not syncing right now.";
    case "unknown":
      return "Can't check: not answering right now.";
  }
}

/** "Today at 9:30 AM" as it reads mid-sentence. */
function lowerDay(when: string): string {
  return when.replace(/^(Today|Yesterday)/, (day) => day.toLowerCase());
}

/** "Syncing, 10 MB of 40 MB." or "Syncing." */
function joined(what: string, progress: string): string {
  return progress === "" ? `${what}.` : `${what}, ${progress}.`;
}

/** The share a folder is in (its copy or its live view), the deepest one when shares nest. */
export function shareAt(connections: Connections | null, folder: string): Share | undefined {
  return (connections?.shares ?? [])
    .filter((share) => share.kind !== "file" && folder.startsWith(targetOf(share)))
    .sort((a, b) => targetOf(b).length - targetOf(a).length)[0];
}

/** What a copy's folder says about its sync, above its entries or in place of them. */
export interface SyncNotice {
  tone: "warning" | "progress";
  title: string;
  detail: string;
  /** Whether it shows only while the folder holds nothing, where it stands in for the "Empty" note. */
  isOnlyWhenEmpty: boolean;
}

/** What to tell someone looking inside `folder` when it is in a copy whose sync has stopped or hasn't finished its
 *  first copy, or null when there is nothing to say. */
export function syncNoticeOf(connections: Connections | null, folder: string): SyncNotice | null {
  const share = shareAt(connections, folder);
  if (connections === null || share === undefined) return null;
  const status = syncStatusOf(share);
  const device = deviceNameOf(connections, share.device);
  if (status?.kind === "failed") {
    return {
      tone: "warning",
      title: `Sync with ${device} stopped`,
      detail: `${status.message === null ? "" : `${status.message} `}Retry it in Imbue Studio on your computer.`,
      isOnlyWhenEmpty: false,
    };
  }
  if (status?.kind === "copying") {
    const progress = progressOf(status.done, status.total);
    return {
      tone: "progress",
      title: `Copying from ${device}`,
      detail: progress === "" ? "Files appear here as they arrive." : `${progress} so far.`,
      isOnlyWhenEmpty: true,
    };
  }
  return null;
}

async function failureOf(response: Response, what: string): Promise<DufsError> {
  const detail = (await response.text().catch(() => "")).trim();
  return new DufsError(detail === "" ? `${what} (${response.status})` : detail, response.status);
}

export async function fetchConnections(signal?: AbortSignal): Promise<Connections> {
  const response = await fetch(CONNECTIONS_API, { signal });
  if (!response.ok) throw await failureOf(response, "Couldn't read your connections");
  return (await response.json()) as Connections;
}
