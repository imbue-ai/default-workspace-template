// The browser-orchestrated create sequence: generate keypair -> claim ->
// persist pending-create -> push sync record (encrypted secrets) -> poll the
// share gateway until healthy -> provision backups over exec -> done. Each
// step is idempotent so a resumed create re-runs from the top safely.

import {
  type ClaimResult,
  ApiError,
  bucketInfo,
  claimHost,
  createBucket,
  rollBucketKey,
  shareStatus,
} from "./api";
import {
  type Ed25519Keypair,
  generateKeypair,
  keypairFromSeed,
  opensshPrivateKeyPem,
  opensshPublicKeyLine,
} from "./crypto/ed25519";
import {
  aeadDecrypt,
  aeadEncrypt,
  base64ToBytes,
  bytesToBase64,
  randomBytes,
} from "./crypto/secretbox";
import {
  type WorkspaceSecretsPayload,
  encryptSecretsPayload,
} from "./crypto/secretbox";
import { currentDek } from "./dekstore";
import {
  ExecClient,
  execOriginFromHealth,
  probeWorkspaceHealth,
  workspaceEntryHost,
} from "./exec";
import {
  type PendingCreate,
  discardPendingCreate,
  pushRecordWithCas,
  savePendingCreate,
} from "./records";

const HEALTH_POLL_INTERVAL_MS = 3000;
const HEALTH_POLL_TIMEOUT_MS = 180_000;
const RESTIC_ENV_REMOTE_PATH = "data/.secrets/restic.env";

export type CreateStepName =
  "keypair" | "claim" | "record" | "health" | "backups" | "done";

export interface CreateProgress {
  step: CreateStepName;
  message: string;
}

export class CreateFlowError extends Error {
  constructor(
    public readonly step: CreateStepName,
    message: string,
  ) {
    super(message);
  }
}

function requireDek(): Uint8Array {
  const dek = currentDek();
  if (dek === null) {
    throw new CreateFlowError(
      "keypair",
      "The account is locked (no data key in this tab)",
    );
  }
  return dek;
}

async function encryptSeed(seed: Uint8Array): Promise<string> {
  return bytesToBase64(await aeadEncrypt(requireDek(), seed));
}

export async function decryptPendingKeypair(
  pending: PendingCreate,
): Promise<Ed25519Keypair> {
  const seed = await aeadDecrypt(
    requireDek(),
    base64ToBytes(pending.encrypted_private_key_b64),
  );
  return keypairFromSeed(seed);
}

function sanitizeHostName(displayName: string): string {
  const slug = displayName
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
  return slug || `workspace-${Date.now() % 100000}`;
}

async function buildSecretsBlob(
  keypair: Ed25519Keypair,
  resticEnv: string | null,
): Promise<string> {
  const payload: WorkspaceSecretsPayload = {
    restic_env: resticEnv,
    ssh_private_key: opensshPrivateKeyPem(keypair, "minds-web"),
    ssh_known_hosts: null,
  };
  return encryptSecretsPayload(requireDek(), payload);
}

async function pushWorkspaceRecord(
  claim: ClaimResult,
  encryptedSecrets: string,
): Promise<void> {
  await pushRecordWithCas(claim.host_id, (stored) => ({
    host_id: claim.host_id,
    agent_id: claim.agent_id,
    display_name: claim.display_name,
    color: stored?.color ?? null,
    provider_kind: "imbue_cloud",
    hosting_device_id: null,
    device_label: "web",
    state: "active",
    restored_from_host_id: stored?.restored_from_host_id ?? null,
    encrypted_secrets: encryptedSecrets,
    revision: 0,
  }));
}

// Wait until the workspace's routable entry origin answers, returning the
// entry label it answered at. The label is recorded server-side only once the
// workspace's tunnel claims its service labels (the frps NewProxy callback),
// so a fresh claim starts without one: re-resolve it from the share status on
// every poll until it appears (the bare domain never routes, so there is
// nothing to probe before then).
async function waitForHealthy(
  hostId: string,
  workspaceDomain: string,
  initialEntryLabel: string | null,
  onProgress: (progress: CreateProgress) => void,
): Promise<string> {
  const deadline = Date.now() + HEALTH_POLL_TIMEOUT_MS;
  let entryLabel = initialEntryLabel;
  for (;;) {
    if (entryLabel === null) {
      const status = await shareStatus(hostId).catch(() => null);
      entryLabel = status?.entry_label ?? null;
    }
    if (entryLabel !== null) {
      const health = await probeWorkspaceHealth(
        workspaceEntryHost(workspaceDomain, entryLabel),
      );
      if (health.reachable) return entryLabel;
    }
    if (Date.now() > deadline) {
      throw new CreateFlowError(
        "health",
        `The workspace at ${workspaceDomain} did not come up within ${HEALTH_POLL_TIMEOUT_MS / 1000}s`,
      );
    }
    onProgress({
      step: "health",
      message: "Waiting for the workspace to come online...",
    });
    await new Promise((resolve) =>
      setTimeout(resolve, HEALTH_POLL_INTERVAL_MS),
    );
  }
}

function renderResticEnv(
  repository: string,
  accessKeyId: string,
  secretAccessKey: string,
  password: string,
): string {
  const lines = [
    "# Written by the minds web client; the workspace's host-backup service reads this.",
    `RESTIC_REPOSITORY=${repository}`,
    `AWS_ACCESS_KEY_ID=${accessKeyId}`,
    `AWS_SECRET_ACCESS_KEY=${secretAccessKey}`,
    `RESTIC_PASSWORD=${password}`,
  ];
  return lines.join("\n") + "\n";
}

// Mint (or re-mint credentials for) the workspace's backup bucket and return
// the canonical restic.env text. The bucket name is the host id, which the
// connector reserves for workspaces with a matching record.
async function mintBackupResticEnv(hostId: string): Promise<string> {
  let bucketName: string;
  let s3Endpoint: string;
  let accessKeyId: string;
  let secretAccessKey: string;
  try {
    const created = await createBucket(hostId);
    bucketName = created.bucket.bucket_name;
    s3Endpoint = created.bucket.s3_endpoint;
    accessKeyId = created.key.access_key_id;
    secretAccessKey = created.key.secret_access_key;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 409) throw error;
    // The bucket already exists (a resumed create): reuse it with a rolled key.
    const info = await bucketInfo(hostId);
    const key = await rollBucketKey(hostId);
    bucketName = info.bucket_name;
    s3Endpoint = info.s3_endpoint;
    accessKeyId = key.access_key_id;
    secretAccessKey = key.secret_access_key;
  }
  const repository = `s3:${s3Endpoint.replace(/\/$/, "")}/${bucketName}`;
  const password = bytesToBase64(randomBytes(24));
  return renderResticEnv(repository, accessKeyId, secretAccessKey, password);
}

// Provision backups over the exec channel. Requires the owner *workspace*
// session (the /_health service map is session-authed), which only exists
// after the workspace has been opened in the chrome once -- so this runs
// from the workspace view, after the iframe's silent owner handoff.
export async function provisionBackupsOverExec(
  claim: ClaimResult,
  keypair: Ed25519Keypair,
  onProgress: (progress: CreateProgress) => void,
): Promise<void> {
  onProgress({
    step: "backups",
    message: "Locating the workspace's exec service...",
  });
  // A pending record persisted before the tunnel came up has no entry label;
  // the share status carries the tunnel-recorded one by the time we get here.
  let entryLabel = claim.entry_label;
  if (entryLabel === null) {
    const status = await shareStatus(claim.host_id).catch(() => null);
    entryLabel = status?.entry_label ?? null;
  }
  const health = await probeWorkspaceHealth(
    workspaceEntryHost(claim.workspace_domain, entryLabel),
  );
  const execOrigin = execOriginFromHealth(claim.workspace_domain, health);
  if (execOrigin === null) {
    throw new CreateFlowError(
      "backups",
      "The workspace is up but its exec service is not discoverable yet " +
        "(open the workspace once so the owner session exists, then resume setup)",
    );
  }
  // Address the inner owner-exec by its host-id-scoped audience. The daemon
  // also accepts the share domain, but container:<host-id> is the going-forward
  // default and works whether or not the workspace is shared.
  const exec = new ExecClient(execOrigin, `container:${claim.host_id}`, keypair);
  onProgress({ step: "backups", message: "Creating the backup bucket..." });
  const resticEnv = await mintBackupResticEnv(claim.host_id);
  onProgress({ step: "backups", message: "Writing backup credentials..." });
  const encoder = new TextEncoder();
  await exec.writeFile(
    RESTIC_ENV_REMOTE_PATH,
    bytesToBase64(encoder.encode(resticEnv)),
    0o600,
  );
  onProgress({
    step: "backups",
    message: "Initializing the backup repository...",
  });
  const result = await exec.run(
    ["python3", "system/scripts/provision_backups.py"],
    { timeoutSeconds: 180 },
  );
  if (result.exitCode !== 0) {
    throw new CreateFlowError(
      "backups",
      `Backup initialization failed (exit ${result.exitCode}): ${result.stderr.slice(-500)}`,
    );
  }
  // Fold the restic env into the synced secrets so the desktop inherits it.
  const encryptedSecrets = await buildSecretsBlob(keypair, resticEnv);
  await pushWorkspaceRecord(claim, encryptedSecrets);
}

export interface CreateFlowResult {
  hostId: string;
  workspaceDomain: string;
  displayName: string;
}

export async function runCreateFlow(
  displayName: string,
  onProgress: (progress: CreateProgress) => void,
): Promise<CreateFlowResult> {
  onProgress({ step: "keypair", message: "Generating the workspace key..." });
  requireDek();
  const keypair = await generateKeypair();
  const publicKeyLine = opensshPublicKeyLine(keypair.publicKey, "minds-web");

  onProgress({ step: "claim", message: "Claiming a workspace..." });
  const claim = await claimHost({
    sshPublicKey: publicKeyLine,
    hostName: sanitizeHostName(displayName),
    displayName,
  });

  const pending: PendingCreate = {
    host_id: claim.host_id,
    host_db_id: claim.host_db_id,
    agent_id: claim.agent_id,
    host_name: claim.host_name,
    display_name: claim.display_name,
    workspace_domain: claim.workspace_domain,
    entry_label: claim.entry_label,
    encrypted_private_key_b64: await encryptSeed(keypair.seed),
    public_key_line: publicKeyLine,
    step: "claimed",
    created_at_iso: new Date().toISOString(),
  };
  await savePendingCreate(pending);

  return finishCreateFlow(pending, keypair, onProgress);
}

// The shared tail of a fresh create and a resumed one.
export async function finishCreateFlow(
  pending: PendingCreate,
  keypair: Ed25519Keypair,
  onProgress: (progress: CreateProgress) => void,
): Promise<CreateFlowResult> {
  const claim: ClaimResult = {
    host_db_id: pending.host_db_id,
    agent_id: pending.agent_id,
    host_id: pending.host_id,
    host_name: pending.host_name,
    display_name: pending.display_name,
    workspace_domain: pending.workspace_domain,
    region: "",
    entry_label: pending.entry_label ?? null,
  };

  onProgress({ step: "record", message: "Syncing the workspace record..." });
  const encryptedSecrets = await buildSecretsBlob(keypair, null);
  await pushWorkspaceRecord(claim, encryptedSecrets);
  await savePendingCreate({ ...pending, step: "record_pushed" });

  onProgress({
    step: "health",
    message: "Waiting for the workspace to come online...",
  });
  const entryLabel = await waitForHealthy(
    pending.host_id,
    pending.workspace_domain,
    pending.entry_label ?? null,
    onProgress,
  );
  // Persist the learned entry label so the backups step (which runs later,
  // from the workspace view) can locate the exec service without re-resolving.
  await savePendingCreate({
    ...pending,
    entry_label: entryLabel,
    step: "waiting_healthy",
  });

  // Backups need the owner workspace session (established by the iframe's
  // silent handoff), so the workspace view finishes that step -- see
  // completePendingSetup. The create lands the user in the workspace now.
  onProgress({ step: "done", message: "Workspace ready" });
  return {
    hostId: pending.host_id,
    workspaceDomain: pending.workspace_domain,
    displayName: pending.display_name,
  };
}

// Finish a pending create's in-workspace setup (backups) from the workspace
// view, once the iframe's owner handoff has established the workspace
// session. Safe to re-run; discards the pending record on success.
export async function completePendingSetup(
  pending: PendingCreate,
  onProgress: (progress: CreateProgress) => void,
): Promise<void> {
  const keypair = await decryptPendingKeypair(pending);
  const claim: ClaimResult = {
    host_db_id: pending.host_db_id,
    agent_id: pending.agent_id,
    host_id: pending.host_id,
    host_name: pending.host_name,
    display_name: pending.display_name,
    workspace_domain: pending.workspace_domain,
    region: "",
    entry_label: pending.entry_label ?? null,
  };
  await provisionBackupsOverExec(claim, keypair, onProgress);
  await discardPendingCreate(pending.host_id);
  onProgress({ step: "done", message: "Workspace setup complete" });
}
