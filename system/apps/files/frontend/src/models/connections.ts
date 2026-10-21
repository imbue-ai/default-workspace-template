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
  /** Whether the copy holds what the computer does; null for a share with no copy. */
  sync_state: "current" | "behind" | "unknown" | null;
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
function shortDeviceName(deviceName: string): string {
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
  if (share.synced_copy === null) return share.is_reachable ? "" : "Not answering right now.";
  switch (share.sync_state) {
    case "behind":
      return "Hasn't caught up yet.";
    case "unknown":
      return "Can't check: not answering right now.";
    default:
      return "Up to date.";
  }
}

/** The share a folder is in (its copy or its live view), the deepest one when shares nest. */
export function shareAt(connections: Connections | null, folder: string): Share | undefined {
  return (connections?.shares ?? [])
    .filter((share) => share.kind !== "file" && folder.startsWith(targetOf(share)))
    .sort((a, b) => targetOf(b).length - targetOf(a).length)[0];
}

/** What to tell someone looking inside `folder` when it is in a copy that has fallen behind its computer, or null
 *  when it is not. */
export function stuckSyncOf(
  connections: Connections | null,
  folder: string,
): { title: string; detail: string } | null {
  const share = shareAt(connections, folder);
  if (connections === null || share === undefined || share.sync_state !== "behind") return null;
  return {
    title: `Not synced yet from ${deviceNameOf(connections, share.device)}`,
    detail:
      "Your computer has files here that haven't arrived. The sync may be stuck: check it in Imbue Studio on your computer.",
  };
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

export async function fetchConnections(signal?: AbortSignal): Promise<Connections> {
  const response = await fetch(CONNECTIONS_API, { signal });
  if (!response.ok) throw await failureOf(response, "Couldn't read your connections");
  return (await response.json()) as Connections;
}
