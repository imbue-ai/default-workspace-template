/**
 * The update notice: the rollback point the last careful-flow apply kept, as the shell reports
 * it (contracts.md section 5). The socket seeds it on connect and pushes every change as
 * ``update_notice_changed``; the two verbs -- "Everything seems good" (confirm) and "Roll back"
 * -- go to the shell's routes, and what they did comes back the same way, so this model holds
 * nothing it computed itself.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";

/** The shell's own app name: what the top banner (rather than a tab's band) keys on. */
export const SHELL_APP_NAME = "system_interface";

/** The record as the shell sends it. */
export interface UpdateNoticeWire {
  merge_sha: string;
  applied_at: number;
  driven_by: string;
  apps: string[];
  programs: string[];
  needs_services_restart: boolean;
  progress: string | null;
  outcome: string | null;
}

export interface UpdateNotice {
  mergeSha: string;
  /** Seconds since the epoch. */
  appliedAt: number;
  drivenBy: string;
  /** The critical apps whose program or bundle the apply changed; their tabs carry the band. */
  apps: string[];
  programs: string[];
  /** The diff reached the workspace's own setup: a rollback restores the files but an agent
   *  must restart the workspace. */
  needsServicesRestart: boolean;
  /** What a running rollback is doing right now. */
  progress: string | null;
  /** How the rollback ended; the notice is settled once set. */
  outcome: string | null;
}

let notice: UpdateNotice | null = null;

export function noticeFromWire(wire: UpdateNoticeWire): UpdateNotice {
  return {
    mergeSha: wire.merge_sha,
    appliedAt: wire.applied_at,
    drivenBy: wire.driven_by,
    apps: wire.apps,
    programs: wire.programs,
    needsServicesRestart: wire.needs_services_restart,
    progress: wire.progress,
    outcome: wire.outcome,
  };
}

/** Take the notice as the socket reports it (``null`` once the record is cleared). */
export function applyUpdateNotice(wire: UpdateNoticeWire | null): void {
  notice = wire === null ? null : noticeFromWire(wire);
}

export function getUpdateNotice(): UpdateNotice | null {
  return notice;
}

/** The notice for one app's tabs (or the shell's banner), or null when it is not among what the apply touched. */
export function updateNoticeForApp(appName: string): UpdateNotice | null {
  if (notice === null || !notice.apps.includes(appName)) return null;
  return notice;
}

export function isRollbackRunning(current: UpdateNotice): boolean {
  return current.progress !== null && current.outcome === null;
}

export function isNoticeSettled(current: UpdateNotice): boolean {
  return current.outcome !== null;
}

/** "Everything seems good", or closing a settled notice: the shell discards the kept copies and the record. */
export async function confirmUpdate(): Promise<void> {
  await postJson<void>(apiUrl("/api/updates/pending/confirm"), {});
}

/** "Roll back": the shell starts the rollback; its progress and outcome arrive on the socket. */
export async function rollbackUpdate(): Promise<void> {
  await postJson<{ detail: string }>(apiUrl("/api/updates/pending/rollback"), {});
}

export function resetUpdateNoticeForTesting(): void {
  notice = null;
}
