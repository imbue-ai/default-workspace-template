/**
 * What the user has shared with this workspace from their computers (the Files server's `/__files/api/connections`,
 * read from the latchkey gateway), and the ways each share reaches the workspace: synced both ways, synced one way
 * from the computer, or read live from the computer with no copy here.
 */

import { ServerError } from "./server";
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
  /** How far the transfer under way has got; both null when none is. */
  bytes_done: number | null;
  bytes_total: number | null;
}

/** How a synced copy stands, as Files says it. */
export type SyncStatus =
  /** ``percent`` is how far the transfer under way has got, when Studio said. */
  | { kind: "syncing"; percent: number | null }
  | { kind: "current" }
  | { kind: "failed"; message: string | null }
  /** Imbue Studio has it switched on but nothing is running: mostly for a moment, as Studio starts and brings its syncs
   *  back up (one it cannot bring back is a failure, with the reason). A switched-off sync never gets here: Files reads
   *  only the syncs Studio has on, so a folder whose sync was switched off is a live share. */
  | { kind: "stalled" }
  | { kind: "unknown" };

/** Whether any share's copy is carrying something across. */
export function isAnySyncMoving(connections: Connections | null): boolean {
  return (connections?.shares ?? []).some((share) => syncStatusOf(share)?.kind === "syncing");
}

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
      return { kind: "stalled" };
    case "SYNCED":
      return { kind: "current" };
    case "STARTING":
    case "RESTARTING":
    case "SYNCING":
      return { kind: "syncing", percent: percentOf(sync.bytes_done, sync.bytes_total) };
  }
}

/** How far a pass has got, in whole percent short of 100 (it says done when it settles), or null when the sync didn't
 *  say. */
function percentOf(done: number | null, total: number | null): number | null {
  if (done === null || total === null || total <= 0) return null;
  return Math.min(99, Math.max(0, Math.floor((done / total) * 100)));
}

export interface Connections {
  devices: Device[];
  shares: Share[];
}

/** The badge a share wears on its icon: a chain when it is read live, arrows when a copy of it is kept here, turning
 *  while that copy is being made or a change carried across. */
export type ShareBadge = "linked" | "synced" | "syncing";

export function shareBadgeOf(share: Share): ShareBadge {
  if (share.synced_copy === null) return "linked";
  return syncStatusOf(share)?.kind === "syncing" ? "syncing" : "synced";
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
function shortDeviceName(deviceName: string): string {
  return deviceName.replace(/\.local$/, "");
}

/** A share's tooltip: one short line naming the computer and how the share stands, and, when there is one, a quieter
 *  second line with the number or reason behind it. Never the share's own name, and never which way it syncs,
 *  which the read-only badge already tells. */
export interface ShareTooltip {
  text: string;
  detail: string | null;
}

export function describeShare(share: Share, device: string): ShareTooltip {
  const status = syncStatusOf(share);
  if (status === null) {
    return { text: share.is_reachable ? `On ${device}` : `${device} isn't answering`, detail: null };
  }
  switch (status.kind) {
    case "syncing":
      return { text: `Syncing with ${device}`, detail: status.percent === null ? null : `${status.percent}% done` };
    case "current":
      return { text: `Up to date with ${device}`, detail: null };
    case "failed":
      return { text: `Sync with ${device} stopped`, detail: status.message?.replace(/\.$/, "") ?? null };
    case "stalled":
      return { text: `Not syncing with ${device} right now`, detail: null };
    case "unknown":
      return { text: `${device} isn't answering`, detail: null };
  }
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

/** What to tell someone looking inside `folder` when it is in a copy whose sync has stopped, or one that is syncing
 *  (said only while the folder is empty, in place of "Empty"), or null when there is nothing to say. */
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
  if (status?.kind === "syncing") {
    return {
      tone: "progress",
      title: `Syncing from ${device}`,
      detail: status.percent === null ? "Files appear here as they arrive" : `${status.percent}% done`,
      isOnlyWhenEmpty: true,
    };
  }
  return null;
}

/** The server refuses with `{"detail": ...}` (app_schema's ErrorResponse); anything else is read as text. */
async function refusalOf(response: Response): Promise<string> {
  const isJson = (response.headers.get("Content-Type") ?? "").includes("application/json");
  if (!isJson) return response.text().catch(() => "");
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  return typeof body?.detail === "string" ? body.detail : "";
}

async function failureOf(response: Response, what: string): Promise<ServerError> {
  const detail = (await refusalOf(response)).trim();
  return new ServerError(detail === "" ? `${what} (${response.status})` : detail, response.status);
}

export async function fetchConnections(): Promise<Connections> {
  const response = await fetch(CONNECTIONS_API);
  if (!response.ok) throw await failureOf(response, "Couldn't read your connections");
  return (await response.json()) as Connections;
}
