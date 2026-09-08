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

/** POST a JSON body and answer the parsed JSON reply, throwing with the server's detail on a refusal. */
export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(await errorDetailFromResponse(response));
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
