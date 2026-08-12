// Workspace-record orchestration: CAS read-modify-write pushes with
// field-ownership merge rules, and the pending-create store that lets an
// interrupted create be resumed or discarded on the next visit.

import {
  type WireRecord,
  RevisionConflictError,
  listRecords,
  putRecord,
} from "./api";
import { openStore } from "./idb";

const MAX_CAS_RETRIES = 3;

// Push one record with CAS retry. `mutate` receives the freshest stored row
// (or null when the record does not exist yet) and returns the desired
// record fields; the revision is managed here. User-intent fields (name,
// color, state) win outright; a caller that wants enrich-only semantics
// implements it inside `mutate` by copying stored values.
export async function pushRecordWithCas(
  hostId: string,
  mutate: (stored: WireRecord | null) => WireRecord,
): Promise<WireRecord> {
  let stored: WireRecord | null = null;
  for (let attempt = 0; attempt < MAX_CAS_RETRIES; attempt++) {
    const desired = mutate(stored);
    const record: WireRecord = {
      ...desired,
      host_id: hostId,
      revision: (stored?.revision ?? 0) + 1,
    };
    try {
      return await putRecord(record);
    } catch (error) {
      if (error instanceof RevisionConflictError && error.stored !== null) {
        stored = error.stored;
        continue;
      }
      if (error instanceof RevisionConflictError) {
        // A conflict without the stored row: re-read the collection.
        const records = await listRecords();
        stored = records.find((r) => r.host_id === hostId) ?? null;
        continue;
      }
      throw error;
    }
  }
  throw new Error(
    `Record push for ${hostId} kept conflicting after ${MAX_CAS_RETRIES} attempts`,
  );
}

// A create that has leased/claimed but not yet finished; persisted so a
// closed tab can be resumed or discarded. The private key travels AEAD
// encrypted under the account DEK -- never plaintext at rest.
export interface PendingCreate {
  host_id: string;
  host_db_id: string;
  agent_id: string;
  host_name: string;
  display_name: string;
  workspace_domain: string;
  // The shell label (routable entry origin is `${entry_label}.${workspace_domain}`).
  entry_label?: string | null;
  encrypted_private_key_b64: string;
  public_key_line: string;
  step: "claimed" | "record_pushed" | "waiting_healthy" | "done";
  created_at_iso: string;
}

export async function savePendingCreate(pending: PendingCreate): Promise<void> {
  const store = await openStore("pending-creates");
  await store.put(pending.host_id, pending);
}

export async function loadPendingCreates(): Promise<PendingCreate[]> {
  const store = await openStore("pending-creates");
  const keys = await store.listKeys();
  const pendings: PendingCreate[] = [];
  for (const key of keys) {
    const value = await store.get<PendingCreate>(key);
    if (value !== undefined) pendings.push(value);
  }
  return pendings;
}

export async function discardPendingCreate(hostId: string): Promise<void> {
  const store = await openStore("pending-creates");
  await store.delete(hostId);
}
