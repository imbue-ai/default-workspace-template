// JSON client for the connector's resource API. All calls are same-origin
// (the chrome is path-served on the connector), authenticated by the
// SuperTokens browser-session cookie, with one transparent session refresh
// on 401 (the same pattern as the accounts frontend).

import type { KeyBundle } from "./crypto/secretbox";

const REFRESH_PATH = "/accounts/auth/session/refresh";

export interface Identity {
  signed_in: boolean;
  user_id?: string;
  email?: string;
  email_verified?: boolean;
}

export interface LeasedHost {
  host_db_id: string;
  vps_address: string;
  ssh_port: number;
  ssh_user: string;
  container_ssh_port: number;
  agent_id: string;
  host_id: string;
  host_name: string;
  attributes: Record<string, unknown>;
  leased_at: string;
}

export interface ClaimResult {
  host_db_id: string;
  agent_id: string;
  host_id: string;
  host_name: string;
  display_name: string;
  workspace_domain: string;
  region: string;
  // The shell service's origin label; the routable entry origin is
  // `${entry_label}.${workspace_domain}` (the bare domain is unrouted on
  // the relay). Null until the workspace's tunnel claims its service labels
  // (the connector's frps NewProxy callback records it).
  entry_label: string | null;
}

export interface WireRecord {
  host_id: string;
  agent_id: string;
  display_name: string;
  color: string | null;
  provider_kind: string;
  hosting_device_id: string | null;
  device_label: string;
  state: string;
  destroyed_at?: string | null;
  restored_from_host_id: string | null;
  encrypted_secrets: string | null;
  revision: number;
}

export interface ShareStatus {
  host_id: string;
  workspace_domain: string;
  state: string;
  is_tunnel_live?: boolean;
  entry_label?: string | null;
}

export interface MintResult {
  key: string;
  base_url: string;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: unknown,
  ) {
    super(`API error ${status}: ${JSON.stringify(detail)}`);
  }
}

export class RevisionConflictError extends Error {
  constructor(public readonly stored: WireRecord | null) {
    super("workspace record revision conflict");
  }
}

async function tryRefreshSession(): Promise<boolean> {
  try {
    const resp = await fetch(REFRESH_PATH, {
      method: "POST",
      credentials: "same-origin",
    });
    return resp.ok;
  } catch {
    return false;
  }
}

async function request(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const doFetch = () => fetch(path, { credentials: "same-origin", ...init });
  const first = await doFetch();
  if (first.status !== 401) return first;
  const refreshed = await tryRefreshSession();
  if (!refreshed) return first;
  return doFetch();
}

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const resp = await request(path, init);
  const body = resp.status === 204 ? null : await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new ApiError(
      resp.status,
      body && (body as { detail?: unknown }).detail,
    );
  }
  return body as T;
}

function jsonInit(method: string, payload: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

export async function fetchIdentity(): Promise<Identity> {
  return requestJson<Identity>("/accounts/api/me");
}

export function loginUrl(): string {
  const next = encodeURIComponent(
    window.location.pathname + window.location.search,
  );
  return `/login?next=${next}`;
}

export async function listHosts(): Promise<LeasedHost[]> {
  return requestJson<LeasedHost[]>("/hosts");
}

export async function claimHost(args: {
  sshPublicKey: string;
  hostName: string;
  displayName: string;
  region?: string;
}): Promise<ClaimResult> {
  return requestJson<ClaimResult>(
    "/hosts/claim",
    jsonInit("POST", {
      ssh_public_key: args.sshPublicKey,
      host_name: args.hostName,
      display_name: args.displayName,
      region: args.region ?? null,
    }),
  );
}

export async function releaseHost(hostDbId: string): Promise<void> {
  await requestJson<{ status: string }>(`/hosts/${hostDbId}/release`, {
    method: "POST",
  });
}

export async function enableSharing(hostDbId: string): Promise<{
  host_id: string;
  workspace_domain: string;
  region: string;
}> {
  return requestJson(`/hosts/${hostDbId}/enable-sharing`, { method: "POST" });
}

export async function listRecords(): Promise<WireRecord[]> {
  const body = await requestJson<{ records: WireRecord[] }>("/sync/records");
  return body.records;
}

// One CAS attempt; a 409 surfaces the stored row so the caller can merge.
export async function putRecord(record: WireRecord): Promise<WireRecord> {
  const resp = await request(
    `/sync/records/${record.host_id}`,
    jsonInit("PUT", record),
  );
  const body = await resp.json().catch(() => null);
  if (resp.status === 409) {
    // The connector shapes conflicts as {"detail": {"message", "stored"}}.
    const detail = (body as { detail?: { stored?: WireRecord } } | null)
      ?.detail;
    throw new RevisionConflictError(detail?.stored ?? null);
  }
  if (!resp.ok) {
    throw new ApiError(
      resp.status,
      body && (body as { detail?: unknown }).detail,
    );
  }
  return body as WireRecord;
}

export async function fetchKeyBundle(): Promise<KeyBundle | null> {
  const resp = await request("/sync/bundle");
  if (resp.status === 404) return null;
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new ApiError(
      resp.status,
      body && (body as { detail?: unknown }).detail,
    );
  }
  return body as KeyBundle;
}

// Another client (tab, device) already stored a bundle: the create-only put
// lost the race. The caller should unlock with the winning password instead.
export class KeyBundleExistsError extends Error {
  constructor() {
    super("a key bundle already exists for this account");
    this.name = "KeyBundleExistsError";
  }
}

export async function putKeyBundle(bundle: KeyBundle): Promise<void> {
  await requestJson("/sync/bundle", jsonInit("PUT", bundle));
}

// Create-only put for first-time setup: exactly one of two racing clients
// wins server-side; the loser gets KeyBundleExistsError and must not keep
// its freshly minted DEK (the stored bundle can never recover it).
export async function putKeyBundleIfAbsent(bundle: KeyBundle): Promise<void> {
  const resp = await request("/sync/bundle?if_absent=true", jsonInit("PUT", bundle));
  if (resp.status === 409) throw new KeyBundleExistsError();
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new ApiError(
      resp.status,
      body && (body as { detail?: unknown }).detail,
    );
  }
}

export async function deleteKeyBundle(): Promise<void> {
  await requestJson("/sync/bundle", { method: "DELETE" });
}

export async function scrubSyncedSecrets(): Promise<void> {
  await requestJson("/sync/scrub-secrets", { method: "POST" });
}

export async function shareStatus(hostId: string): Promise<ShareStatus | null> {
  const resp = await request(`/shares/${hostId}/status`);
  if (resp.status === 404) return null;
  const body = await resp.json().catch(() => null);
  if (!resp.ok) {
    throw new ApiError(
      resp.status,
      body && (body as { detail?: unknown }).detail,
    );
  }
  return body as ShareStatus;
}

export async function mintWorkspaceKey(hostId: string): Promise<MintResult> {
  return requestJson<MintResult>(
    "/keys/workspace-mint",
    jsonInit("POST", { host_id: hostId }),
  );
}

export interface BucketKeyMaterial {
  access_key_id: string;
  secret_access_key: string;
}

export interface CreatedBucket {
  bucket: { bucket_name: string; s3_endpoint: string };
  key: BucketKeyMaterial;
}

export async function createBucket(shortName: string): Promise<CreatedBucket> {
  return requestJson<CreatedBucket>(
    "/buckets",
    jsonInit("POST", { name: shortName, access: "readwrite" }),
  );
}

export async function bucketInfo(
  shortName: string,
): Promise<{ bucket_name: string; s3_endpoint: string }> {
  return requestJson(`/buckets/${shortName}`);
}

export async function rollBucketKey(
  shortName: string,
): Promise<BucketKeyMaterial> {
  const rolled = await requestJson<
    { key: BucketKeyMaterial } | BucketKeyMaterial
  >(`/buckets/${shortName}/roll-key`, { method: "POST" });
  return "key" in rolled ? rolled.key : rolled;
}
