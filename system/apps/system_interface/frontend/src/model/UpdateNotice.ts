/**
 * The update notice: the rollback point the last update-app careful-flow apply kept, as the shell
 * reports it (desktop contracts.md section 5). The socket seeds it on connect and pushes every
 * change as ``update_notice_changed``, which the store keeps in the desktop state; the two verbs
 * -- "Everything seems good" (confirm) and "Roll back" -- go to the shell's routes, and what they
 * did comes back the same way, so nothing here is computed on the client.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { postJson } from "@imbue/workspace-ui/src/models/http";
import type { UpdateNoticeWire } from "./records";

/** The shell's own app name, which the banner words as the workspace interface. */
export const SHELL_APP_NAME = "system_interface";

export interface UpdateNotice {
  readonly mergeSha: string;
  /** Seconds since the epoch. */
  readonly appliedAt: number;
  readonly drivenBy: string;
  /** The critical apps whose program or bundle the apply changed; the banner names them. */
  readonly apps: readonly string[];
  readonly programs: readonly string[];
  /** The diff reached the workspace's own setup: a rollback restores the files but an agent
   *  must restart the workspace's system services. */
  readonly needsSystemServicesRestart: boolean;
  /** What a running rollback is doing right now. */
  readonly progress: string | null;
  /** How the rollback ended; the notice is settled once set. */
  readonly outcome: string | null;
}

export function noticeFromWire(wire: UpdateNoticeWire): UpdateNotice {
  return {
    mergeSha: wire.merge_sha,
    appliedAt: wire.applied_at,
    drivenBy: wire.driven_by,
    apps: wire.apps,
    programs: wire.programs,
    needsSystemServicesRestart: wire.needs_system_services_restart,
    progress: wire.progress,
    outcome: wire.outcome,
  };
}

/** Whether the apply touched no app's program or bundle: a change to how the workspace starts, say. */
export function isWorkspaceOnlyNotice(current: UpdateNotice): boolean {
  return current.apps.length === 0;
}

export function isRollbackRunning(current: UpdateNotice): boolean {
  return current.progress !== null && current.outcome === null;
}

export function isNoticeSettled(current: UpdateNotice): boolean {
  return current.outcome !== null;
}

/** "Everything seems good", or closing a settled notice: the shell drops the record, and the kept copies with
 *  it when no rollback ran (a failed rollback's copies stay for an agent). */
export async function confirmUpdate(): Promise<void> {
  await postJson<void>(apiUrl("/api/updates/pending/confirm"), {});
}

/** "Roll back": the shell starts the rollback; its progress and outcome arrive on the socket. */
export async function rollbackUpdate(): Promise<void> {
  await postJson<{ detail: string }>(apiUrl("/api/updates/pending/rollback"), {});
}
