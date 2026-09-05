/**
 * Client layouts: one arrangement per view per client, read and written through the shell's
 * layout routes (contracts.md section 6).
 *
 * A layout is the serialized dockview grid plus one tab record per panel: which address the
 * panel shows, the tab id the shell minted for it, and when it was last the active one. The
 * shell falls back to a per-device seed (the last arrangement any client saved on that device
 * kind) and then to the empty layout, so a new client on a view starts where the last one was.
 */

import type { SerializedDockview } from "dockview-core";
import { apiUrl } from "../base-path";
import { getDeviceKind } from "./ClientIdentity";
import { errorDetailFromResponse } from "./http";

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
const TAB_ID_HEX_LENGTH = 16;
const TAB_ID_PATTERN = /^tab-[0-9a-f]{16}$/;

/** A fresh tab id: ``tab-<16 hex>``, never reused. */
export function mintTabId(): string {
  const bytes = new Uint8Array(TAB_ID_HEX_LENGTH / 2);
  if (typeof crypto !== "undefined" && "getRandomValues" in crypto) {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  return `${TAB_ID_PREFIX}${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

export function isTabId(value: string): boolean {
  return TAB_ID_PATTERN.test(value);
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

/** Save this client's arrangement of ``viewId``. Throws on failure (callers treat autosave as
 *  best-effort and catch). */
export async function saveLayout(
  viewId: string,
  clientId: string,
  dockview: SerializedDockview | null,
  tabs: Record<string, TabRecord>,
): Promise<void> {
  const response = await fetch(apiUrl(`/api/layouts/${encodeURIComponent(viewId)}`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId, device_kind: getDeviceKind(), dockview, tabs }),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
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
