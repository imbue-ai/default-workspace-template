/**
 * Measured transcript geometry kept by the workspace rather than by one browser.
 *
 * The second tier behind `geometryCache`, which is this browser's own IndexedDB
 * copy. Both hold the same thing -- what some client measured a conversation's
 * rows to be at a given viewport width -- and they differ only in who can see
 * it: a conversation opened in a different window, a different browser, or on a
 * different device reserves its scroll space from what was already measured
 * instead of settling in from an estimate.
 *
 * The server stores and returns; it never derives a height. A row is a whole
 * turn, and only a client that rendered one knows how tall it came out (see
 * `imbue/system_interface/transcript_geometry.py`).
 *
 * Every call degrades silently, exactly as `MemberLastUsed` does: geometry is an
 * optimisation, so an unreachable or older server must read as "nothing measured
 * yet" and leave the transcript rendering from its own measurements.
 */

import { apiUrl } from "../base-path";
import type { GeometrySnapshot } from "./rowGeometry";

function geometryUrl(agentId: string): string {
  return apiUrl(`/api/agents/${encodeURIComponent(agentId)}/geometry`);
}

/**
 * Whether a width bucket names a viewport the server will file geometry under.
 *
 * `widthBucketFor` quantizes, so a panel narrower than half a bucket rounds to
 * zero -- which is no viewport at all, and which the server rejects rather than
 * storing measurements nothing could have taken. Checked here so a collapsed
 * panel costs no request instead of a 400.
 */
function isStorableBucket(widthBucket: number): boolean {
  return Number.isInteger(widthBucket) && widthBucket > 0;
}

/**
 * What this workspace last measured for one conversation at one width, or null
 * when nothing has been.
 */
export async function loadWorkspaceGeometry(agentId: string, widthBucket: number): Promise<GeometrySnapshot | null> {
  if (!isStorableBucket(widthBucket)) {
    return null;
  }
  try {
    const response = await fetch(`${geometryUrl(agentId)}?width=${widthBucket}`);
    if (!response.ok) {
      return null;
    }
    const data = (await response.json()) as Partial<GeometrySnapshot>;
    if (!Array.isArray(data.rows) || data.rows.length === 0) {
      return null;
    }
    // Handed on unvalidated: `geometryFromSnapshot` is the one place that
    // decides what counts as a row, so a shape change degrades to re-measuring
    // rather than being rejected differently at each tier.
    return { rows: data.rows };
  } catch {
    return null;
  }
}

/**
 * File what this client measured, replacing whatever the workspace held for that
 * conversation at that width. Fire-and-forget: a failed write costs one settling
 * pass on the next visit and nothing else.
 */
export async function saveWorkspaceGeometry(
  agentId: string,
  widthBucket: number,
  snapshot: GeometrySnapshot,
): Promise<void> {
  if (!isStorableBucket(widthBucket) || snapshot.rows.length === 0) {
    return;
  }
  try {
    await fetch(geometryUrl(agentId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ width: widthBucket, rows: snapshot.rows }),
    });
  } catch {
    // An unreachable server must never disturb a paint.
  }
}
