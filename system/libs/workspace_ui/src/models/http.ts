/** What the workspace's REST routes answer, read the way the user needs it said. */

import { failureText, readFailure, type Failure, type RequestKind } from "./failure";

/**
 * A refused or unanswered request to a REST route, as ``readFailure`` read it. Its ``message`` is ``failureText``'s
 * (the server's reason, for the log and the screens not yet worded); a surface words it with ``failureSentence``.
 */
export class HttpError extends Error {
  constructor(readonly failure: Failure) {
    super(failureText(failure));
  }

  /** The HTTP status, or 0 when nothing answered. */
  get status(): number {
    return this.failure.status ?? 0;
  }

  /** The JSON object the server answered, for a caller that reads a field of its own; null when there was none. */
  get body(): Readonly<Record<string, unknown>> | null {
    return this.failure.body;
  }
}

/** The failure a non-2xx ``response`` stands for. */
export async function readResponseFailure(response: Response, requestKind: RequestKind): Promise<HttpError> {
  const text = await response.text().catch(() => "");
  let body: unknown = null;
  try {
    body = JSON.parse(text);
  } catch {
    // Not JSON (an error page, a proxy's plain text): quoted behind "See details".
  }
  return new HttpError(readFailure({ status: response.status, body, rawText: text, requestKind }));
}

/** ``fetch`` that answers a request nothing answered (the connection refused or dropped) as an ``HttpError``. */
export async function fetchOrNotAnswered(url: string, init: RequestInit, requestKind: RequestKind): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (error) {
    throw new HttpError(readFailure({ status: 0, body: null, rawText: (error as Error).message, requestKind }));
  }
}

/** GET ``url`` and answer the parsed JSON reply, throwing an ``HttpError`` on a refusal or no answer. */
export async function getJson<T>(url: string): Promise<T> {
  const response = await fetchOrNotAnswered(url, {}, "read");
  if (!response.ok) throw await readResponseFailure(response, "read");
  return (await response.json()) as T;
}

/** POST a JSON body and answer the parsed JSON reply, throwing an ``HttpError`` on a refusal or no answer. */
export async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetchOrNotAnswered(
    url,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    "change",
  );
  if (!response.ok) throw await readResponseFailure(response, "change");
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
