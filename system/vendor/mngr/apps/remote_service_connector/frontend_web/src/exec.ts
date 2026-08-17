// The owner-exec client: SSH-equivalent authority over a workspace from the
// browser. Every request is signed per the RFC 9421/9530 strict profile (see
// crypto/ed25519.ts) and rides the share stack cross-origin with the workspace
// session cookie attached (credentials: include; the gateway's forward_auth
// checks the owner session and the service verifies the signature). Every
// response (and the /run stream trailer) is verified against the endpoint's
// pinned SSH host key, bound to the request -- so a compromised container
// cannot tamper with results either.

import {
  type Ed25519Keypair,
  type ExecStreamTrailer,
  type PinnedHostKey,
  signExecRequest,
  verifyExecResponse,
  verifyExecStreamTrailer,
} from "./crypto/ed25519";
import { bytesToBase64, randomBytes } from "./crypto/secretbox";

const textEncoder = new TextEncoder();

function nowUnix(): number {
  return Math.floor(Date.now() / 1000);
}

// The signed request context needed to verify the response it produced.
interface SignedFetch {
  response: Response;
  method: string;
  path: string;
  signatureHeader: string;
  signatureB64: string;
}

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
    // The endpoint's audience binding: "container:<host-id>" (inner) or
    // "vm:<host-id>" (vm). The inner daemon also accepts the workspace share
    // domain, but container:<host-id> is the going-forward default.
    private readonly audience: string,
    private readonly keypair: Ed25519Keypair,
    // The endpoint's pinned SSH host key (from the claim response / synced
    // record). When set, every response and stream trailer is verified against
    // it and the client fails closed on a bad signature. When omitted,
    // verification is skipped -- a transitional affordance until the connector
    // threads the VM host key through claim; production must supply it.
    private readonly hostKey?: PinnedHostKey,
  ) {}

  private async signedFetch(
    method: string,
    path: string,
    body: Uint8Array,
  ): Promise<SignedFetch> {
    const nonce = bytesToBase64(randomBytes(18));
    const { headers, signatureB64 } = await signExecRequest(
      this.keypair,
      method,
      path,
      body,
      this.audience,
      nowUnix(),
      nonce,
    );
    const response = await fetch(`${this.baseUrl}${path}`, {
      method,
      credentials: "include",
      headers: { ...headers, "Content-Type": "application/json" },
      body: method === "GET" ? undefined : (body as BodyInit),
    });
    return {
      response,
      method,
      path,
      signatureHeader: headers.Signature,
      signatureB64,
    };
  }

  // Verify a completed response against the pinned host key (no-op when no host
  // key is configured). Returns the response body bytes.
  private async verifiedBody(signed: SignedFetch): Promise<Uint8Array> {
    const bodyBytes = new Uint8Array(await signed.response.arrayBuffer());
    if (this.hostKey) {
      await verifyExecResponse(
        signed.response.status,
        signed.response.headers,
        bodyBytes,
        {
          method: signed.method,
          path: signed.path,
          signatureHeader: signed.signatureHeader,
        },
        this.hostKey,
        nowUnix(),
      );
    }
    return bodyBytes;
  }

  async isAlive(): Promise<boolean> {
    try {
      const resp = await fetch(`${this.baseUrl}/_alive`, {
        credentials: "include",
      });
      return resp.status === 200;
    } catch {
      return false;
    }
  }

  // Run a command, collecting the NDJSON stream into one result. `onEvent`
  // (when given) sees each event as it arrives, for progress display. Output
  // is unverified until the signed trailer is checked against the host key at
  // the end; a missing or bad trailer throws (when a host key is configured).
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
    const signed = await this.signedFetch("POST", "/run", body);
    const resp = signed.response;
    if (!resp.ok || resp.body === null) {
      throw new Error(`exec run failed: HTTP ${resp.status}`);
    }
    const result: ExecRunResult = { stdout: "", stderr: "", exitCode: null };
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffered = "";
    // The trailer signs the exact stream bytes (each non-trailer event line
    // plus its newline), so accumulate them as the server hashed them.
    let streamText = "";
    let trailer: ExecStreamTrailer | null = null;
    const consume = (line: string) => {
      if (!line.trim()) return;
      const event = JSON.parse(line) as ExecRunEvent | ExecStreamTrailer;
      if ((event as ExecStreamTrailer).type === "signature") {
        trailer = event as ExecStreamTrailer;
        return;
      }
      streamText += `${line}\n`;
      const runEvent = event as ExecRunEvent;
      options.onEvent?.(runEvent);
      if (runEvent.type === "stdout") result.stdout += runEvent.data ?? "";
      if (runEvent.type === "stderr") result.stderr += runEvent.data ?? "";
      if (runEvent.type === "exit") result.exitCode = runEvent.code ?? null;
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
    if (this.hostKey) {
      if (trailer === null) {
        throw new Error("exec run ended without a signed trailer");
      }
      await verifyExecStreamTrailer(
        textEncoder.encode(streamText),
        signed.signatureB64,
        trailer,
        this.hostKey,
        nowUnix(),
      );
    }
    return result;
  }

  async readFile(
    path: string,
  ): Promise<{ exists: boolean; contentB64: string }> {
    const body = textEncoder.encode(JSON.stringify({ path }));
    const signed = await this.signedFetch("POST", "/read-file", body);
    const bodyBytes = await this.verifiedBody(signed);
    if (signed.response.status === 404) return { exists: false, contentB64: "" };
    if (!signed.response.ok) {
      throw new Error(`exec read-file failed: HTTP ${signed.response.status}`);
    }
    const parsed = JSON.parse(new TextDecoder().decode(bodyBytes)) as {
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
    // The service's wire contract carries mode as an octal STRING (parsed with
    // base 8), matching the Python client; serialize the numeric mode as octal
    // rather than sending a JSON number (which the daemon would reject).
    if (mode !== undefined) payload.mode = mode.toString(8);
    const body = textEncoder.encode(JSON.stringify(payload));
    const signed = await this.signedFetch("POST", "/write-file", body);
    await this.verifiedBody(signed);
    if (!signed.response.ok) {
      throw new Error(`exec write-file failed: HTTP ${signed.response.status}`);
    }
  }

  async getGrants(): Promise<GrantsDocument> {
    const signed = await this.signedFetch("GET", "/grants", new Uint8Array(0));
    const bodyBytes = await this.verifiedBody(signed);
    if (!signed.response.ok) {
      throw new Error(`exec get-grants failed: HTTP ${signed.response.status}`);
    }
    const parsed = JSON.parse(new TextDecoder().decode(bodyBytes)) as {
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
    const signed = await this.signedFetch("PUT", "/grants", body);
    const bodyBytes = await this.verifiedBody(signed);
    if (signed.response.status === 409) {
      const conflict = JSON.parse(new TextDecoder().decode(bodyBytes)) as {
        revision: string;
        grants_toml: string;
      };
      throw new GrantsConflictError({
        grantsToml: conflict.grants_toml,
        revision: conflict.revision,
      });
    }
    if (!signed.response.ok) {
      throw new Error(`exec put-grants failed: HTTP ${signed.response.status}`);
    }
    const parsed = JSON.parse(new TextDecoder().decode(bodyBytes)) as {
      revision: string;
    };
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
