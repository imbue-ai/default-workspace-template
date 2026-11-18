/** What the shell's REST routes answer, read the way the user needs it said. */

// What the tunnel in front of this server answers when the server itself is not up: a
// workspace still provisioning, restarting, or shutting down. The request never reached an
// endpoint, so nothing was read and nothing changed.
const UNREACHABLE_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

/**
 * Why a request failed, in terms the user can act on: the server's own ``detail`` when it gave
 * one, a plain "not responding" for a gateway answer, and the bare status otherwise.
 */
export async function errorDetailFromResponse(response: Response): Promise<string> {
  const data = (await response.json().catch(() => ({}))) as { detail?: string; error?: string };
  if (data.detail) return data.detail;
  if (data.error) return data.error;
  if (UNREACHABLE_STATUSES.has(response.status)) {
    return "the workspace is not responding right now, so nothing was changed — try again in a moment";
  }
  return `HTTP ${response.status}`;
}

/** A refusal from a REST route: the server's detail as the message, and the status for a caller
 *  that tells one refusal from another (a stale save's 409, say). */
export class HttpError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
  }
}

/**
 * A JSON API call: the method, a JSON body when one is given, and the parsed JSON reply (nothing for a 204),
 * throwing an ``HttpError`` with the server's detail on a refusal. Never served from the browser's cache: an
 * API answer is read for what the server says now.
 */
export async function requestJson<T>(method: string, url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    throw new HttpError(response.status, await errorDetailFromResponse(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** POST a JSON body and answer the parsed JSON reply, throwing an ``HttpError`` with the server's detail on a refusal. */
export function postJson<T>(url: string, body: unknown): Promise<T> {
  return requestJson<T>("POST", url, body);
}
