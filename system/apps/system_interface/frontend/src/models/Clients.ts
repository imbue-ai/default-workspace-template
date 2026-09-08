/**
 * The shell's client records (contracts.md section 6): one per browser context, carrying the view
 * it is on. The record is the source of a client's active view, so a window reads its own record
 * on boot rather than keeping the view in local storage (two windows of one browser are one client
 * and land on the same view).
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface ClientRecord {
  id: string;
  device_kind: string;
  active_view: string;
  last_seen: string;
  is_connected: boolean;
}

/** Every client the shell knows. Null when the shell could not answer (logged), which is not an
 *  empty list: a caller falls back to its own defaults. */
export async function fetchClients(): Promise<ClientRecord[] | null> {
  try {
    const response = await fetch(apiUrl("/api/clients"));
    if (!response.ok) {
      console.warn(`[si] could not list clients: HTTP ${response.status}`);
      return null;
    }
    const data = (await response.json()) as { clients?: ClientRecord[] };
    return data.clients ?? [];
  } catch (e) {
    console.warn("[si] could not list clients", e);
    return null;
  }
}

/** The view this client was last on, from its record; "" when the shell has none for it. */
export async function fetchOwnActiveView(clientId: string): Promise<string> {
  const clients = await fetchClients();
  return clients?.find((client) => client.id === clientId)?.active_view ?? "";
}
