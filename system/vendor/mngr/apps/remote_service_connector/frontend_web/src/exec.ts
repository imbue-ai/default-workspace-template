// The owner-exec client: SSH-equivalent authority over a workspace from the
// browser. Every request is Ed25519-signed (see crypto/ed25519.ts) and rides
// the share stack cross-origin with the workspace session cookie attached
// (credentials: include; the gateway's forward_auth checks the owner session
// and the service verifies the signature).

import { type Ed25519Keypair, signExecEnvelope } from "./crypto/ed25519";
import { bytesToBase64, randomBytes } from "./crypto/secretbox";

const textEncoder = new TextEncoder();

export interface ExecRunEvent {
  type: "stdout" | "stderr" | "exit";
  data?: string;
  code?: number;
}

export interface ExecRunResult {
  stdout: string;
  stderr: string;
  exitCode: number | null;
}

export class ExecClient {
  constructor(
    // e.g. https://owner-exec-ab12cd34.<workspace_domain>
    private readonly baseUrl: string,
    // The workspace's share domain -- the envelope's audience binding.
    private readonly audience: string,
    private readonly keypair: Ed25519Keypair,
  ) {}

  private async signedFetch(
    method: string,
    path: string,
    body: Uint8Array,
  ): Promise<Response> {
    const timestamp = String(Math.floor(Date.now() / 1000));
    const nonce = bytesToBase64(randomBytes(18));
    const headers = await signExecEnvelope(
      this.keypair,
      method,
      path,
      body,
      this.audience,
      timestamp,
      nonce,
    );
    return fetch(`${this.baseUrl}${path}`, {
      method,
      credentials: "include",
      headers: { ...headers, "Content-Type": "application/json" },
      body: method === "GET" ? undefined : (body as BodyInit),
    });
  }

  async isAlive(): Promise<boolean> {
    try {
      const resp = await fetch(`${this.baseUrl}/_alive`, {
        credentials: "include",
      });
      return resp.status === 204;
    } catch {
      return false;
    }
  }

  // Run a command, collecting the NDJSON stream into one result. `onEvent`
  // (when given) sees each event as it arrives, for progress display.
  async run(
    command: string[],
    options: {
      cwd?: string;
      timeoutSeconds?: number;
      onEvent?: (event: ExecRunEvent) => void;
    } = {},
  ): Promise<ExecRunResult> {
    const payload: Record<string, unknown> = { command };
    if (options.cwd !== undefined) payload.cwd = options.cwd;
    if (options.timeoutSeconds !== undefined) {
      payload.timeout_seconds = options.timeoutSeconds;
    }
    const body = textEncoder.encode(JSON.stringify(payload));
    const resp = await this.signedFetch("POST", "/run", body);
    if (!resp.ok || resp.body === null) {
      throw new Error(`exec run failed: HTTP ${resp.status}`);
    }
    const result: ExecRunResult = { stdout: "", stderr: "", exitCode: null };
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffered = "";
    const consume = (line: string) => {
      if (!line.trim()) return;
      const event = JSON.parse(line) as ExecRunEvent;
      options.onEvent?.(event);
      if (event.type === "stdout") result.stdout += event.data ?? "";
      if (event.type === "stderr") result.stderr += event.data ?? "";
      if (event.type === "exit") result.exitCode = event.code ?? null;
    };
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffered += decoder.decode(value, { stream: true });
      const lines = buffered.split("\n");
      buffered = lines.pop() ?? "";
      for (const line of lines) consume(line);
    }
    if (buffered) consume(buffered);
    return result;
  }

  async readFile(
    path: string,
  ): Promise<{ exists: boolean; contentB64: string }> {
    const body = textEncoder.encode(JSON.stringify({ path }));
    const resp = await this.signedFetch("POST", "/read-file", body);
    if (resp.status === 404) return { exists: false, contentB64: "" };
    if (!resp.ok) throw new Error(`exec read-file failed: HTTP ${resp.status}`);
    const parsed = (await resp.json()) as {
      exists: boolean;
      content_b64: string;
    };
    return { exists: parsed.exists, contentB64: parsed.content_b64 };
  }

  async writeFile(
    path: string,
    contentB64: string,
    mode?: number,
  ): Promise<void> {
    const payload: Record<string, unknown> = { path, content_b64: contentB64 };
    if (mode !== undefined) payload.mode = mode;
    const body = textEncoder.encode(JSON.stringify(payload));
    const resp = await this.signedFetch("POST", "/write-file", body);
    if (!resp.ok)
      throw new Error(`exec write-file failed: HTTP ${resp.status}`);
  }

  async getGrants(): Promise<GrantsDocument> {
    const resp = await this.signedFetch("GET", "/grants", new Uint8Array(0));
    if (!resp.ok)
      throw new Error(`exec get-grants failed: HTTP ${resp.status}`);
    const parsed = (await resp.json()) as {
      grants_toml: string;
      revision: string;
    };
    return { grantsToml: parsed.grants_toml, revision: parsed.revision };
  }

  // Replace the grants document. Pass the revision from a prior getGrants()
  // as `baseRevision` so a concurrent edit surfaces as GrantsConflictError
  // (carrying the current document to merge and retry against) instead of
  // being silently overwritten; omit it only for deliberate blind resets.
  async putGrants(
    grantsToml: string,
    baseRevision?: string,
  ): Promise<{ revision: string }> {
    const payload: Record<string, unknown> = { grants_toml: grantsToml };
    if (baseRevision !== undefined) payload.base_revision = baseRevision;
    const body = textEncoder.encode(JSON.stringify(payload));
    const resp = await this.signedFetch("PUT", "/grants", body);
    if (resp.status === 409) {
      const conflict = (await resp.json()) as {
        revision: string;
        grants_toml: string;
      };
      throw new GrantsConflictError({
        grantsToml: conflict.grants_toml,
        revision: conflict.revision,
      });
    }
    if (!resp.ok)
      throw new Error(`exec put-grants failed: HTTP ${resp.status}`);
    const parsed = (await resp.json()) as { revision: string };
    return { revision: parsed.revision };
  }
}

export interface GrantsDocument {
  grantsToml: string;
  // Opaque compare-and-swap token for the document (sha256 of the file
  // bytes; "" while no grants file exists yet).
  revision: string;
}

// A putGrants() lost the compare-and-swap race: another writer replaced the
// document after our read. Carries the current document so the caller can
// merge and retry.
export class GrantsConflictError extends Error {
  constructor(readonly current: GrantsDocument) {
    super("grants document changed since it was read (CAS conflict)");
    this.name = "GrantsConflictError";
  }
}

export interface WorkspaceHealth {
  reachable: boolean;
  detail: {
    gateway?: string;
    backend?: string;
    owner?: boolean;
    services?: Record<string, string>;
  } | null;
}

// The routable host to enter/probe a workspace at: the shell label origin
// when known, else the bare domain (which only routes if a future share
// design claims it -- kept as the graceful-degradation fallback).
export function workspaceEntryHost(
  workspaceDomain: string,
  entryLabel: string | null | undefined,
): string {
  return entryLabel ? `${entryLabel}.${workspaceDomain}` : workspaceDomain;
}

// Emitted once, before the first health probe, so the transient console
// errors that probe produces while a workspace is still coming online are not
// mistaken for real bugs. A cross-origin fetch to a workspace whose share
// stack (relay tunnel + gateway TLS cert) is not up yet fails at the transport
// layer, and the browser logs that failure itself -- as "CORS request did not
// succeed" with a null status code -- no matter how the fetch is caught. We
// catch it, treat it as not-ready, and keep polling; the errors stop the
// moment the workspace answers. There is no way to suppress the browser's own
// network-error logging from page JS, so this note is the mitigation.
let _hasExplainedHealthProbeErrors = false;

function _explainHealthProbeErrorsOnce(): void {
  if (_hasExplainedHealthProbeErrors) return;
  _hasExplainedHealthProbeErrors = true;
  console.info(
    "[minds] Polling workspaces' /_health to detect when they come online. " +
      "While a workspace is still starting, its cross-origin /_health is not reachable yet, " +
      'so the browser will log expected, harmless errors like "Cross-Origin Request Blocked: ' +
      'the Same Origin Policy disallows reading the remote resource at https://<service>.<workspace>/_health ' +
      '(Reason: CORS request did not succeed). Status code: (null)". ' +
      "These are retried automatically and stop once the workspace answers; they are not a bug.",
  );
}

// Probe a workspace's share-gateway /_health: 204 = alive (no session yet);
// 200 + JSON = session-authed detail (owner detail includes the service
// label map, which is how the exec origin is discovered).
export async function probeWorkspaceHealth(
  probeHost: string,
): Promise<WorkspaceHealth> {
  _explainHealthProbeErrorsOnce();
  try {
    const resp = await fetch(`https://${probeHost}/_health`, {
      credentials: "include",
    });
    if (resp.status === 204) return { reachable: true, detail: null };
    if (resp.ok) {
      return {
        reachable: true,
        detail: (await resp.json()) as WorkspaceHealth["detail"],
      };
    }
    return { reachable: false, detail: null };
  } catch {
    return { reachable: false, detail: null };
  }
}

export function execOriginFromHealth(
  workspaceDomain: string,
  health: WorkspaceHealth,
): string | null {
  const label = health.detail?.services?.["owner-exec"];
  return label ? `https://${label}.${workspaceDomain}` : null;
}
