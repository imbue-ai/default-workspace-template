/**
 * Client layouts: one arrangement per view per client, read and written through the shell's
 * layout routes (contracts.md section 6).
 *
 * A layout is the serialized dockview grid plus one tab record per panel: which address the
 * panel shows, the tab id the shell minted for it, and when it was last the active one. The
 * client's layout file on the shell is the truth of the arrangement: this window writes it for
 * the user's own gestures, the shell writes it for agent ops, and every write is announced as
 * ``layout_updated`` so the client's other windows refetch. Each save carries a save id this
 * window minted (so it can skip the echo of its own writes) and the stamp of the arrangement it
 * was based on (so a save over a newer arrangement is refused rather than clobbering it).
 */

import type { SerializedDockview } from "dockview-core";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { getDeviceKind } from "@imbue/workspace-ui/src/models/ClientIdentity";
import { errorDetailFromResponse } from "@imbue/workspace-ui/src/models/http";

/** What one panel of a layout shows. */
export interface TabRecord {
  address: string;
  tab_id: string;
  last_focused_ms: number;
}

/** One client's arrangement of one view. */
export interface LayoutRecord {
  dockview: SerializedDockview | null;
  tabs: Record<string, TabRecord>;
  device_kind: string;
  updated_at: string | null;
}

const TAB_ID_PREFIX = "tab-";
const SAVE_ID_PREFIX = "save-";
const MINTED_ID_HEX_LENGTH = 16;
// How many of this window's own save ids are remembered for echo suppression; the broadcast of
// a save arrives within a round trip, so a short memory is plenty.
const REMEMBERED_SAVE_IDS = 64;

const HTTP_CONFLICT = 409;

/** The shell refused a save because the stored arrangement is newer than the one it was based on. */
export class StaleLayoutSaveError extends Error {}

function mintHex(): string {
  const bytes = new Uint8Array(MINTED_ID_HEX_LENGTH / 2);
  if (typeof crypto !== "undefined" && "getRandomValues" in crypto) {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** A fresh tab id: ``tab-<16 hex>``, never reused. */
export function mintTabId(): string {
  return `${TAB_ID_PREFIX}${mintHex()}`;
}

const ownSaveIds: string[] = [];

/** A fresh save id for one save this window makes, remembered so the save's own broadcast is skipped. */
export function mintSaveId(): string {
  const saveId = `${SAVE_ID_PREFIX}${mintHex()}`;
  ownSaveIds.push(saveId);
  if (ownSaveIds.length > REMEMBERED_SAVE_IDS) ownSaveIds.splice(0, ownSaveIds.length - REMEMBERED_SAVE_IDS);
  return saveId;
}

/** Whether this window minted ``saveId`` (the echo of its own save). */
export function isOwnSaveId(saveId: string): boolean {
  return ownSaveIds.includes(saveId);
}

/** Fetch this client's arrangement of ``viewId`` (the seed, or the empty layout, when it has
 *  none). Throws when the shell could not answer: that is not an empty layout, and a caller
 *  must not save over the real one. */
export async function fetchLayout(viewId: string, clientId: string): Promise<LayoutRecord> {
  const query = `client=${encodeURIComponent(clientId)}&device=${encodeURIComponent(getDeviceKind())}`;
  const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(viewId)}?${query}`));
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  return (await response.json()) as LayoutRecord;
}

/** What a save answered: the stamp the shell wrote, or null when the save changed nothing. */
export interface LayoutSaveOutcome {
  updatedAt: string | null;
}

/** Save this client's arrangement of ``viewId``, based on the arrangement stamped ``baseUpdatedAt``
 *  (null for one only ever seen empty). Throws ``StaleLayoutSaveError`` when the shell holds a newer
 *  arrangement, and a plain error for any other refusal (callers treat autosave as best-effort). */
export async function saveLayout(
  viewId: string,
  clientId: string,
  dockview: SerializedDockview | null,
  tabs: Record<string, TabRecord>,
  baseUpdatedAt: string | null,
): Promise<LayoutSaveOutcome> {
  const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(viewId)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: clientId,
      save_id: mintSaveId(),
      base_updated_at: baseUpdatedAt,
      device_kind: getDeviceKind(),
      dockview,
      tabs,
    }),
  });
  if (response.status === HTTP_CONFLICT) {
    throw new StaleLayoutSaveError(await errorDetailFromResponse(response));
  }
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  const answer = (await response.json()) as { updated_at: string | null };
  return { updatedAt: answer.updated_at };
}

/** The panels of a layout whose tab record names an address no longer listed, so a restore
 *  can drop them (the observation that prunes references, contracts.md section 4.1). */
export function panelsWithUnlistedAddresses(
  tabs: Readonly<Record<string, TabRecord>>,
  isListed: (address: string) => boolean,
): string[] {
  return Object.entries(tabs)
    .filter(([, tab]) => !isListed(tab.address))
    .map(([panelId]) => panelId);
}
